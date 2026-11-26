"""Ghost-text autocomplete: asks a backend to continue the text at the caret. No GTK.

Everything runs on the main loop except the job handed to `run_bg`, the only place the
supervisor's blocking calls are made. The text is shielded before it reaches the supervisor.
Tab and Ctrl+Right are grabbed only while a suggestion is on screen.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from glance.audience.resolver import AudienceResolver
from glance.backends.base import BackendError
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.clock import Clock, Handle
from glance.config import Config
from glance.daemon.controller import Controller
from glance.log import log_event
from glance.privacy.redact import shield
from glance.protocol import ApplyResult, Edit, FieldSnapshot, text_sha256
from glance.ui.hotkeys import HotkeyPort
from glance.ui.ports import GhostPort

T = TypeVar("T")

AC_TAB, AC_WORD = "ac_tab", "ac_word"
AC_CHORDS = {AC_TAB: "Tab", AC_WORD: "Control+Right"}
MIN_PREFIX_CHARS = 15
# The complete hotkey's own key grab blurs the app on the press (X sends a NotifyGrab focus
# change) and gives the same field back on the release; so does Ctrl+Right's. A blur this soon
# after either is that one: the completion (or the rest of it, after one word was taken)
# stays, waiting up to BLUR_GRACE_MS for the field to come back.
HOTKEY_BLUR_S = 0.5
BLUR_GRACE_MS = 2000
MAX_CONTEXT_CHARS = 2000
MAX_COMPLETION_CHARS = 200
WATCHDOG_MS = 10_000
FALLBACK_WIDTH_PX = 600  # no field or window rectangle to measure against

_PLACEHOLDER_OPEN = "[[GLANCE_"
_FENCE = re.compile(r"\A```[^\n]*\n(.*?)\n?```\Z", re.DOTALL)
_QUOTE_PAIRS = (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"))
# The end of a sentence: terminators, any closing quotes or brackets, then a space or the end.
_SENTENCE_END = re.compile(r"[.!?…।]+[\"'”’)\]]*(?=\s|\Z)")
_FIRST_WORD = re.compile(r"\s*\S+")
# Where the completion meets the text: no space before _CLOSERS; one between a word or one of
# _AFTER and the next word (or one of _OPENERS).
_CLOSERS = ",.;:!?)]}"
_AFTER = ",.;:!?)"
_OPENERS = "\"“([{"


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _echo_len(prefix: str, body: str) -> int:
    """Length of the copy of the prefix's tail that `body` starts with; 0 when there is none.

    Only a tail of two words or more that starts at a word boundary counts: a shared letter or
    a shared short word ("to" then "tomorrow") is how a real continuation looks too.
    """
    tail = prefix.rstrip()
    for start in range(len(tail)):
        if start and not tail[start - 1].isspace():
            continue
        if tail[start].isspace():
            continue
        candidate = tail[start:]
        if not any(ch.isspace() for ch in candidate):
            return 0  # only single words are left
        if body.startswith(candidate):
            return len(candidate)
    return 0


def clean_completion(raw: str, prefix: str) -> str | None:
    """The text to show after `prefix` for a model's raw answer; None when there is none.

    One line, one sentence, at most 200 characters, no echo of the prefix, no quotes or code
    fence around it, and joined to the prefix by exactly as much space as the two need. An
    answer that names a shielding placeholder or the input wrapper is refused outright.
    """
    if _PLACEHOLDER_OPEN in raw or "glance_text>" in raw:
        return None
    body = raw.strip()
    m = _FENCE.match(body)
    if m:
        body = m.group(1).strip()
    spaced = raw[:1] in (" ", "\t") and not m  # the model's own space before its first word
    for open_q, close_q in _QUOTE_PAIRS:
        inner = body[1:-1]
        if (len(body) >= 2 and body[0] == open_q and body[-1] == close_q
                and open_q not in inner and close_q not in inner):
            body, spaced = inner, inner[:1] == " "
            break
    body = body.lstrip()
    echoed = _echo_len(prefix, body)
    if echoed:
        body = body[echoed:]
        spaced = body[:1].isspace()
        body = body.lstrip()
    body = body.split("\n", 1)[0].rstrip()
    m = _SENTENCE_END.search(body)
    if m:
        body = body[:m.end()]
    if not body:
        return None
    last = prefix[-1:]
    lead = ""
    if last and not last.isspace() and body[0] not in _CLOSERS:
        starts_word = _is_word(body[0]) or body[0] in _OPENERS
        if spaced or (starts_word and (_is_word(last) or last in _AFTER)):
            lead = " "
    out = lead + body
    if len(out) > MAX_COMPLETION_CHARS:
        space = out.rfind(" ", len(lead) + 1, MAX_COMPLETION_CHARS + 1)
        out = out[:space if space > 0 else MAX_COMPLETION_CHARS].rstrip()
    return out


def context_for(before: str) -> str:
    """What the backend gets for the text before the caret: shielded, its last 2000 characters.

    Shielding comes first, so a secret the limit cuts in two is never sent in part; a
    placeholder the cut lands in is dropped whole.
    """
    shielded = shield(before)
    cut = max(0, len(shielded.text) - MAX_CONTEXT_CHARS)
    for start, end, _o0, _o1, placeholder in shielded.segments:
        if placeholder and start < cut < end:
            cut = end
            break
    return shielded.text[cut:]


def eligible(s: FieldSnapshot, min_prefix: int = 1) -> bool:
    """May a completion be shown at this caret? Only at the end of a line (ruling P3-2).

    `min_prefix` is how much text must come before the caret: something to continue at
    least, and `MIN_PREFIX_CHARS` before Glance offers a completion nobody asked for.
    """
    if s.gated or s.caret_rect is None or not min_prefix <= s.caret <= len(s.text):
        return False
    if not s.text[:s.caret].strip():
        return False
    return s.caret == len(s.text) or s.text[s.caret] == "\n"


def _same_place(a: FieldSnapshot, b: FieldSnapshot) -> bool:
    return (a.field_id, a.text, a.caret) == (b.field_id, b.text, b.caret)


@dataclass(frozen=True)
class _Suggestion:
    snap: FieldSnapshot  # the text and caret the completion continues
    text: str


class _Job:
    """One completion call in flight. `cancelled` is set on the main loop, read by the job."""

    def __init__(self, snap: FieldSnapshot, gen: int, live: bool) -> None:
        self.snap = snap
        self.gen = gen
        self.live = live
        self.cancelled = False
        self.blurred = False  # survived the hotkey's blur: the generation moved on meanwhile


@dataclass(frozen=True)
class _Awaited:
    """An accepted word is being typed into the field; `rest` is shown once it has arrived."""

    apply_id: str
    field_id: str
    head: str  # the text before the caret the word was accepted at
    word: str
    tail: str
    rest: str

    def arrived(self, s: FieldSnapshot) -> bool:
        return (s.field_id == self.field_id and s.text == self.head + self.word + self.tail
                and s.caret == len(self.head) + len(self.word))

    def on_the_way(self, s: FieldSnapshot) -> bool:
        """`s` shows the word partly typed (or typed, with the caret yet to follow)."""
        if s.field_id != self.field_id or not s.text.startswith(self.head):
            return False
        end = len(s.text) - len(self.tail)
        return (end >= len(self.head) and s.text[end:] == self.tail
                and self.word.startswith(s.text[len(self.head):end]))


class Autocomplete:
    """Shows a continuation of the text at the caret; Tab takes it, Ctrl+Right one word.

    On demand (`trigger()`, the `complete` hotkey) it works with every backend. Live, it asks
    by itself `autocomplete_idle_ms` after the last text change, in the supervisor's
    speculative lane, when `supervisor.live_autocomplete()` and `auto_allowed()` say so.

    A suggestion belongs to one text and caret: anything else in the field, a lost focus, a
    private field, a pause or ten seconds on screen removes it and releases both keys.
    """

    def __init__(
        self,
        controller: Controller,
        supervisor: BackendSupervisor,
        resolver: AudienceResolver,
        ghost: GhostPort,
        hotkeys: HotkeyPort,
        clock: Clock,
        run_bg: Callable[[Callable[[], T], Callable[[T], None]], None],
        cfg: Callable[[], Config],
        language: Callable[[str], bool] = lambda text: False,
    ) -> None:
        self._c = controller
        self._supervisor = supervisor
        self._resolver = resolver
        self._ghost = ghost
        self._hotkeys = hotkeys
        self._clock = clock
        self._run_bg = run_bg
        self._cfg = cfg
        self._language = language
        self._snap: FieldSnapshot | None = None  # the focused field; None if private or paused
        self._shown: _Suggestion | None = None
        self._job: _Job | None = None
        self._awaited: _Awaited | None = None
        self._idle: Handle | None = None
        self._watchdog: Handle | None = None
        self._hotkey_at: float | None = None  # when `trigger` last ran
        self._grace: Handle | None = None  # the field blurred by the hotkey is awaited

    def attach(self) -> None:
        self._c.add_listener(self)
        self._c.extra_status.append(lambda: {"autocomplete_visible": self.visible})

    @property
    def visible(self) -> bool:
        return self._shown is not None

    # ------------------------------------------------------------------ user actions

    def trigger(self) -> None:
        """The on-demand hotkey: complete at the caret now, whatever the backend."""
        s = self._snap
        if s is None or not eligible(s):
            return
        self.dismiss()
        self._hotkey_at = self._clock.now()
        self._start(s, live=False)

    def accept_all(self) -> None:
        shown = self._shown
        if shown is not None:
            self._accept(shown, shown.text)

    def accept_word(self) -> None:
        shown = self._shown
        if shown is None:
            return
        m = _FIRST_WORD.match(shown.text)
        word = m.group() if m else shown.text
        apply_id = self._accept(shown, word)
        rest = shown.text[len(word):]
        if apply_id and rest.strip():
            s = shown.snap
            self._awaited = _Awaited(apply_id, s.field_id, s.text[:s.caret], word,
                                     s.text[s.caret:], rest)
            # Ctrl+Right is a key grab: its press blurs the app like the complete hotkey's.
            self._hotkey_at = self._clock.now()

    def dismiss(self) -> None:
        """Remove the suggestion, release both keys and stop the call in flight."""
        self._cancel_grace()
        self._hide()
        self._awaited = None
        job, self._job = self._job, None
        if job is not None:
            job.cancelled = True
            if job.live:
                self._supervisor.cancel_speculative()
            else:
                self._supervisor.cancel_all()

    # ------------------------------------------------------------------ controller listener

    def on_snapshot(self, s: FieldSnapshot) -> None:
        if s.gated:  # the controller reports those as a lost focus; never complete one
            self._leave()
            return
        self._cancel_grace()  # whatever comes back ends the wait; the checks below decide
        prev, self._snap = self._snap, s
        typed = prev is not None and prev.field_id == s.field_id and prev.text != s.text
        awaited = self._awaited
        if awaited is not None:
            if awaited.arrived(s):
                self._awaited = None
                if eligible(s):
                    self._present(_Suggestion(s, awaited.rest))
                return
            if awaited.on_the_way(s):
                return
        at = self._shown.snap if self._shown is not None else \
            self._job.snap if self._job is not None else None
        if awaited is not None or (at is not None and not _same_place(at, s)):
            self.dismiss()
        if typed:
            self._cancel_idle()
            self._idle = self._clock.call_later(
                self._cfg().general.autocomplete_idle_ms, self._on_idle)
        elif prev is None or prev.field_id != s.field_id:
            self._cancel_idle()

    def on_focus_lost(self, reason: str = "") -> None:
        at, job = self._hotkey_at, self._job
        if (reason == "blur" and at is not None and self._clock.now() - at <= HOTKEY_BLUR_S
                and (self._shown is not None or self._awaited is not None
                     or (job is not None and not job.live))):
            self._hotkey_at = None  # one blur per press
            if job is not None:
                job.blurred = True
            self._cancel_idle()
            self._cancel_grace()
            self._grace = self._clock.call_later(BLUR_GRACE_MS, self._leave)
            return
        self._leave()

    def on_capture_lost(self) -> None:
        self._leave()

    def on_reset(self) -> None:
        self._leave()

    def on_pause_changed(self, paused: bool) -> None:
        if paused:
            self._leave()

    def on_apply_result(self, r: ApplyResult) -> None:
        awaited = self._awaited
        if awaited is not None and r.apply_id == awaited.apply_id and r.status != "verified":
            self.dismiss()  # the word did not go in: its remainder has nowhere to go

    # ------------------------------------------------------------------ requesting

    def _on_idle(self) -> None:
        self._idle = None
        s = self._snap
        if (s is None or self.visible or self._job is not None or self._awaited is not None
                or not eligible(s, MIN_PREFIX_CHARS)):
            return
        if getattr(self._c, "checks_on_request", False):
            return  # `[general] check = "shortcut"`: nothing is asked of an AI while typing
        if self._supervisor.live_autocomplete():  # cheap: the setting and the active backend
            self._start(s, live=True)

    def _start(self, s: FieldSnapshot, live: bool) -> None:
        self._job = job = _Job(s, self._c.generation, live)
        before = s.text[:s.caret]

        def work() -> str | None:
            try:
                if job.cancelled or (live and not self._supervisor.auto_allowed()):
                    return None
                prefix = context_for(before)  # the backend never sees anything else
                ctx = self._resolver.ctx(s, self._resolver.resolve(s),
                                         mixed_language=bool(self._language(before)))
                if job.cancelled:
                    return None
                return "".join(self._supervisor.complete(prefix, ctx, speculative=live))
            except (AiUnavailable, BackendError):
                return None  # the supervisor logged it
            except Exception as e:  # noqa: BLE001 - the job must always come back
                log_event("complete_error", kind=type(e).__name__)
                return None

        self._run_bg(work, lambda raw: self._done(job, raw))

    def _done(self, job: _Job, raw: str | None) -> None:
        if self._job is not job:
            return  # cancelled or replaced: its text is no longer what the user has
        self._job = None
        s = self._snap
        if (raw is None or s is None or (job.gen != self._c.generation and not job.blurred)
                or not _same_place(job.snap, s) or not eligible(s)):
            return
        text = clean_completion(raw, s.text[:s.caret][-MAX_CONTEXT_CHARS:])
        if text is not None:
            self._present(_Suggestion(s, text))

    # ------------------------------------------------------------------ showing, hiding

    def _present(self, suggestion: _Suggestion) -> None:
        self._hide()
        s = suggestion.snap
        caret = s.caret_rect
        if caret is None:
            return
        box = s.field_rect or s.window_rect
        width = FALLBACK_WIDTH_PX if box is None else box.x + box.w - caret.x
        font_px = int(caret.h * self._cfg().ui.ghost_font_factor)
        # From here on every way out goes through `_hide`, so the keys cannot stay grabbed.
        self._shown = suggestion
        self._watchdog = self._clock.call_later(WATCHDOG_MS, self.dismiss)
        try:
            grabbed = self._hotkeys.grab(AC_TAB, AC_CHORDS[AC_TAB], self.accept_all)
            if grabbed:
                self._hotkeys.grab(AC_WORD, AC_CHORDS[AC_WORD], self.accept_word)
                self._ghost.show(suggestion.text, caret.x + caret.w, caret.y, font_px,
                                 max(1, width))
        except Exception as e:  # noqa: BLE001 - e.g. the X connection is gone
            log_event("ghost_error", kind=type(e).__name__)
            grabbed = False
        if not grabbed:  # nothing to accept it with: a ghost would only be in the way
            self._hide()

    def _hide(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._shown is None:
            return
        self._shown = None
        for step in (lambda: self._hotkeys.ungrab(AC_TAB), lambda: self._hotkeys.ungrab(AC_WORD),
                     self._ghost.hide):
            try:
                step()
            except Exception as e:  # noqa: BLE001 - the other two must still happen
                log_event("ghost_error", kind=type(e).__name__)

    def _accept(self, shown: _Suggestion, text: str) -> str:
        """Insert `text` at the suggestion's caret; the apply id, "" when nothing was sent.

        The ghost and both key grabs go first (ruling P3-4): the write-back path then waits
        for the user's own keys to be released before it types.
        """
        self.dismiss()
        s = shown.snap
        return self._c.request_apply(Edit(s.caret, s.caret, text), "auto", text_sha256(s.text))

    def _cancel_grace(self) -> None:
        if self._grace is not None:
            self._grace.cancel()
            self._grace = None

    def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None

    def _leave(self) -> None:
        """No field, a private one, or paused: nothing to complete and nothing on screen."""
        self._snap = None
        self._cancel_idle()
        self._cancel_grace()
        self.dismiss()
