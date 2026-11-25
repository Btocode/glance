"""AI coordinator: tone analysis and tone rewrites, plugged into the Controller through its
hooks. No GTK.

Everything here runs on the main loop except the jobs handed to `run_bg`, which are the only
place the supervisor's blocking calls (`auto_allowed`, `status`, `analyze`, `rewrite`) are
made. The user's text is shielded before it reaches the supervisor; results live in memory
only: analyses in the LRU cache keyed by a hash, rewrites for as long as the exact text they
were made for stays in the focused field.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from glance.audience.resolver import AudienceResolver, Resolved
from glance.backends.anchor import analysis_to_issues
from glance.backends.base import Analysis, BackendError
from glance.backends.prompts import TONES
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.clock import Clock, Handle
from glance.daemon.cache import LruCache, cache_key
from glance.daemon.controller import Controller
from glance.daemon.rewrite import RewriteOutcome, partial_text, rewrite_text
from glance.diffing import DiffOp, word_diff
from glance.features.sharpener import blanks
from glance.grammar.issues import Issue
from glance.log import log_event
from glance.privacy.redact import Shielded, shield
from glance.protocol import ApplyResult, Edit, FieldSnapshot, text_sha256
from glance.ui.panel_model import PanelView, ToneChip
from glance.ui.ports import OverlayPort
from glance.ui.strip_model import StripView

T = TypeVar("T")

_PLACEHOLDER = re.compile(r"\[\[GLANCE_([SE])\d+\]\]")
HIDDEN = "[hidden]"  # shown where the model's prose mentions a secret's placeholder

CUSTOM = "custom"
ASK_PROMPT = TONES[CUSTOM].label  # "Rewrite as…"
HOTKEY_CHIPS = 4  # Super+Alt+1..4
SPECULATE_IDLE_MS = 3000  # idle time before the first chips are pre-generated
SPECULATE_CHIPS = 2
RENDER_MS = 100  # a streaming preview is redrawn at most this often
# How long a field may be out of sight and still get the rewrite that was asked for it: the
# "Rewrite as…" entry takes the focus, and so, for a moment, does every global hotkey (the
# key grab makes the app report a blur). It must come back as the same field, same text.
AWAIT_FIELD_MS = 2000
AWAIT_COPY_MS = 60_000  # the same, when the rewrite will only be offered as a copy
NO_CHANGE = "No change suggested"
_COPY = "Copy the rewrite instead."
BANNERS = {
    "no_change": NO_CHANGE,
    "limit": "AI paused (limit)",
    "none": "Connect an AI: run `glance backend list` to see how",
    "battery": "AI is on demand only while on battery",
    "failed": "The rewrite did not work. Try again.",
    "placeholders": "The rewrite lost a mention, link or other protected part. " + _COPY,
    "objects": "This field holds embedded content a rewrite cannot keep. " + _COPY,
    "rich": "Typing this back would flatten mentions, links or emoji here. " + _COPY,
}
COPY_ONLY = "Glance could not get back to your window. " + _COPY
# Outcomes worth remembering for the text they were made for; the rest may work next time.
_KEEP = (None, "no_change", "placeholders", "objects", "rich")


@dataclass(frozen=True)
class _Entry:
    """What the cache holds for one analysed text: offsets index the original text."""

    analysis: Analysis
    issues: tuple[Issue, ...]


class _Job:
    """One analyze call in flight. `cancelled` is set on the main loop, read by the job."""

    def __init__(self, gen: int, key: str) -> None:
        self.gen = gen
        self.key = key
        self.spinning = False
        self.cancelled = False


@dataclass(frozen=True)
class _Target:
    """The exact text a rewrite is for. A result for any other target is dropped."""

    field_id: str
    gen: int
    sha: str


class _Rewrite:
    """One rewrite of the focused field's text: in flight, or done and on show.

    `partial` is written by the job's thread and read on the main loop; everything else
    belongs to the main loop.
    """

    def __init__(self, target: _Target, tone: str, custom: str | None, original: str) -> None:
        self.target = target
        self.tone = tone
        self.custom = custom
        self.original = original
        self.state = "loading"  # loading | ready | failed
        self.partial = ""
        self.last_render: float | None = None
        self.shown: tuple[DiffOp, ...] | None = None
        self.outcome: RewriteOutcome | None = None
        self.apply_when_done = False
        self.copy_only = False  # show and offer as a copy, never type it
        self.cancelled = False
        self.finished: tuple[RewriteOutcome, str] | None = None  # done while out of sight

    @property
    def key(self) -> str:
        return _tone_key(self.tone, self.custom)


class _Speculation:
    def __init__(self, target: _Target) -> None:
        self.target = target
        self.cancelled = False


@dataclass(frozen=True)
class _Awaited:
    """A "Rewrite as…" answer waiting for its field to be the focused one again."""

    field_id: str
    sha: str
    custom: str
    apply: bool
    until: float


def _tone_key(tone: str, custom: str | None) -> str:
    return f"{CUSTOM}:{custom}" if tone == CUSTOM else tone


def _display(text: str, shielded: Shielded) -> str:
    """Model prose for the user's own screen: entities restored, secrets stay hidden."""
    originals = dict(shielded.mapping)

    def sub(m: re.Match[str]) -> str:
        return originals.get(m.group(), HIDDEN) if m.group(1) == "E" else HIDDEN

    return _PLACEHOLDER.sub(sub, text)


def _entry(shielded: Shielded, analysis: Analysis) -> _Entry:
    """Anchor the issues on the shielded text the model saw, then map them back."""
    issues: dict[str, Issue] = {}
    for i in analysis_to_issues(shielded.text, analysis):
        span = shielded.map_span(i.start, i.end)
        if span is None or any(_PLACEHOLDER.search(r) for r in i.replacements):
            continue  # touches or invents a placeholder: it could not be applied exactly
        start, end = span
        issue = replace(i, id=f"llm:{i.rule}:{start}:{end}", start=start, end=end,
                        message=_display(i.message, shielded))
        issues.setdefault(issue.id, issue)
    reason = None if analysis.reason is None else _display(analysis.reason, shielded)
    return _Entry(replace(analysis, reason=reason), tuple(issues.values()))


class AiCoordinator:
    """Runs `analyze` when the Controller says one is due and shows the result.

    A result belongs to the exact text and audience it was made for: it is shown whenever
    that text is in the focused field again (cache), and never for any other text.

    `on_ai_status` is where the supervisor's `on_status` callback must land, posted to the
    main loop. The chip and `glance status` show the last status seen, never a fresh
    (blocking) one.

    Rewrites follow the same rule, more strictly: a rewrite belongs to one field, one
    scheduler generation and one text hash. A result that arrives for anything else is
    dropped; nothing is ever typed that the focused field's current text was not the source
    of. Rewrites start only on `preview` / `apply` (hover, click, hotkey), except the
    speculative ones, which need `supervisor.speculate()` and run in the supervisor's
    second lane.

    `overlay` is needed for "Rewrite as…" and for copying; `rich(snapshot)` says whether
    the field is a rich editor, where protected entities cannot be typed back.
    """

    def __init__(
        self,
        controller: Controller,
        supervisor: BackendSupervisor,
        resolver: AudienceResolver,
        cache: LruCache,
        clock: Clock,
        run_bg: Callable[[Callable[[], T], Callable[[T], None]], None],
        language: Callable[[str], bool] = lambda t: False,
        overlay: OverlayPort | None = None,
        rich: Callable[[FieldSnapshot], bool] = lambda s: False,
        writeback: Callable[[FieldSnapshot], str] = lambda s: "auto",
    ) -> None:
        self._c = controller
        self._supervisor = supervisor
        self._resolver = resolver
        self._cache = cache
        self._clock = clock
        self._run_bg = run_bg
        self._language = language
        self._overlay = overlay
        self._rich = rich
        self._writeback = writeback  # the field's app `writeback` setting
        self._ai = "off"  # off (not known yet) | ok | none | limit
        self._probing = False
        self._overrides: dict[str, str] = {}  # context key -> profile name, this session
        self._resolved: Resolved | None = None
        self._shown: tuple[FieldSnapshot, _Entry] | None = None  # for the current text
        self._job: _Job | None = None
        self._rw: _Rewrite | None = None  # the preview
        self._made_for: _Target | None = None
        self._made: dict[str, RewriteOutcome] = {}  # tone key -> outcome, for `_made_for`
        self._offer: tuple[str, str] | None = None  # (field id, rewrite) to copy instead
        self._applied: tuple[str, str, str] | None = None  # (apply id, field id, rewrite)
        self._awaited: _Awaited | None = None
        self._away: _Rewrite | None = None  # in flight; its field went out of sight
        self._away_timer: Handle | None = None
        self._seen: tuple[str, str] | None = None  # (field id, text) of the last snapshot
        self._changed_at = 0.0
        self._spec: _Speculation | None = None
        self._spec_timer: Handle | None = None

    # ------------------------------------------------------------------ wiring

    def attach(self) -> None:
        """Install the hooks (appending: other extensions keep theirs) and learn the status."""
        c = self._c
        earlier = c.on_analyze_due

        def due(s: FieldSnapshot, gen: int) -> None:
            if earlier is not None:
                earlier(s, gen)
            self._on_analyze_due(s, gen)

        c.on_analyze_due = due
        c.extra_chip_inputs.append(self._chip_inputs)
        c.extra_panel.append(self._panel)
        c.extra_strip.append(self._strip)
        c.extra_issues.append(self._issues)
        c.extra_ignored.append(self._ignored)
        c.extra_status.append(self._status)
        c.add_listener(self)
        self.refresh_status()

    # ------------------------------------------------------------------ public

    @property
    def resolver(self) -> AudienceResolver:
        """The audience resolver, shared with the features that rewrite outside a field."""
        return self._resolver

    def resolved(self) -> Resolved | None:
        """The audience of the focused field; None without one (or a private one)."""
        return self._resolved

    def analysis(self) -> Analysis | None:
        """The analysis of exactly the text now in the focused field, if there is one."""
        entry = self._entry_for(self._c.current)
        return None if entry is None else entry.analysis

    def set_profile_override(self, name: str | None) -> None:
        """Use profile `name` for the focused field's context until told otherwise (None).

        Lasts for this session; remembering it is the caller's business.
        """
        if name is not None and self._resolver.profile(name) is None:
            raise ValueError(f"unknown profile: {name}")
        s = self._c.current
        if s is None or s.gated or self._resolved is None:
            return
        if name is None:
            self._overrides.pop(self._resolved.key, None)
        else:
            self._overrides[self._resolved.key] = name
        self._cancel()
        self._made_for = None  # the audience is part of what a rewrite was made for
        self.on_snapshot(s)
        if self._shown is None:
            self._on_analyze_due(s, self._c.generation)
        self._c.refresh()

    def on_ai_status(self, status: str) -> None:
        """The supervisor's status changed. Main loop only."""
        if status != self._ai:
            self._ai = status
            self._c.refresh()

    def refresh_status(self) -> None:
        """Ask the supervisor for its status in the background (it may run health checks)."""
        if self._probing:
            return
        self._probing = True

        def done(status: str) -> None:
            self._probing = False
            self.on_ai_status(status)

        self._run_bg(self._probe, done)

    # ------------------------------------------------------------------ controller listener

    def on_snapshot(self, s: FieldSnapshot) -> None:
        self._resolved = r = self._resolve(s)
        entry = self._cache.get(self._key(s, r)) if r.profile.check else None
        self._shown = None if entry is None else (s, entry)
        if self._seen != (s.field_id, s.text):
            self._seen = (s.field_id, s.text)
            self._changed_at = self._clock.now()
        if self._offer is not None and self._offer[0] != s.field_id:
            self._offer = None
        if self._awaited is not None or self._away is not None:
            # The scheduler has not seen this snapshot yet: let it count the change first.
            self._clock.call_later(0, self._field_is_back)

    def on_typing(self) -> None:
        self._cancel()

    def on_focus_lost(self, _reason: str) -> None:
        self._leave(keep_rewrite=True)

    def on_capture_lost(self) -> None:
        self._awaited = None
        self._leave()

    def on_reset(self) -> None:
        self._awaited = None
        self._leave()

    def on_pause_changed(self, paused: bool) -> None:
        if paused:
            self._awaited = None
            self._leave()

    def on_apply_result(self, r: ApplyResult) -> None:
        applied = self._applied
        if applied is None or r.apply_id != applied[0]:
            return
        self._applied = None
        if r.status == "reverted":
            # The app changed what was typed and it was undone: the rewrite is still wanted.
            # (Shown for this field only; forgotten as soon as another one is focused.)
            self._offer = (applied[1], applied[2])
            s = self._c.current
            if s is not None and s.field_id == applied[1]:
                self._c.open_panel()
                self._c.refresh()

    # ------------------------------------------------------------------ controller providers

    def _chip_inputs(self, s: FieldSnapshot) -> dict[str, Any]:
        out: dict[str, Any] = {"ai": self._ai}
        job = self._job
        if job is not None and job.spinning and job.gen == self._c.generation:
            out["analyzing"] = True
        rw = self._current_rewrite()
        if rw is not None and rw.state == "loading":
            out["analyzing"] = True  # a hotkey rewrite has no panel to show it is working
        entry = self._entry_for(s)
        if entry is not None:
            out["tone_label"] = entry.analysis.tone.capitalize()
            out["mismatch"] = entry.analysis.mismatch
        return out

    def _panel(self, s: FieldSnapshot, panel: PanelView) -> PanelView:
        entry = self._entry_for(s)
        if entry is not None:
            panel = replace(panel, tone=entry.analysis.tone.capitalize(),
                            tone_reason=entry.analysis.reason)
        r = self._resolved
        if r is not None:
            panel = replace(
                panel, tone_chips=self._with_previews(self.tone_chips()),
                profile_label=r.profile.label,
                profiles=tuple((p.name, p.label) for p in self._resolver.profiles()))
        rw = self._current_rewrite()
        if rw is not None:
            panel = replace(panel, preview=rw.shown, preview_state=rw.state,
                            can_apply=self._can_apply(rw))
            banner = self._banner(rw)
            if banner is not None:
                panel = replace(panel, banner=banner)
        offer = self._copy_text(s)
        if offer is not None:
            panel = replace(panel, copy_offer=offer)
        return panel

    def _strip(self, s: FieldSnapshot, view: StripView) -> StripView:
        """Nothing to fix but the tone is off for the audience: one line says so."""
        entry, r = self._entry_for(s), self._resolved
        if view.segments or entry is None or r is None or not entry.analysis.mismatch:
            return view
        tone = entry.analysis.tone.lower()
        return replace(view, note=f"Reads {tone} for {r.profile.label.lower()} — "
                                  "⋯ for tone options")

    def _with_previews(self, chips: tuple[ToneChip, ...]) -> tuple[ToneChip, ...]:
        """Each chip with the rewrite in its tone as far as it is known: the one streaming or
        on show, or one made earlier for this exact text. Nothing is asked for here."""
        rw = self._current_rewrite()
        made = self._made if self._made_for is not None and self._made_for == self._target() \
            else {}
        out = []
        for chip in chips:
            if rw is not None and rw.key == chip.key:
                text = None if rw.shown is None else \
                    "".join(op.text for op in rw.shown if op.kind != "del")
                chip = replace(chip, preview=text, state=rw.state)
            elif chip.key in made and made[chip.key].text is not None:
                chip = replace(chip, preview=made[chip.key].text, state="ready")
            out.append(chip)
        return tuple(out)

    def _issues(self, s: FieldSnapshot) -> list[Issue]:
        entry = self._entry_for(s)
        return [] if entry is None else list(entry.issues)

    def _ignored(self, _s: FieldSnapshot) -> frozenset[str]:
        r = self._resolved
        return frozenset() if r is None else frozenset(r.profile.ignore_rules)

    def _status(self) -> dict[str, Any]:
        r = self._resolved
        return {"ai": self._ai, "backend": self._supervisor.active() or "",
                "profile": "" if r is None else r.profile.name}

    # ------------------------------------------------------------------ analyze

    def _on_analyze_due(self, s: FieldSnapshot, gen: int) -> None:
        r = self._resolve(s)
        if not r.profile.check:
            return
        key = self._key(s, r)
        entry = self._cache.get(key)
        if entry is not None:
            self._shown = (s, entry)
            self._arm_speculation()
            self._c.refresh()
            return
        self._cancel_analysis()
        self._arm_speculation()
        self._job = job = _Job(gen, key)
        text = s.text

        def work() -> tuple[_Entry | None, str]:
            found = None
            try:
                if self._supervisor.auto_allowed() and not job.cancelled:
                    self._clock.post(lambda: self._spin(job))
                    shielded = shield(text)  # the backend never sees anything else
                    ctx = self._resolver.ctx(s, r, mixed_language=bool(self._language(text)))
                    found = _entry(shielded, self._supervisor.analyze(shielded.text, ctx))
            except (AiUnavailable, BackendError):
                pass  # the supervisor logged it; the status below is what the chip shows
            except Exception as e:  # noqa: BLE001 - the spinner must always be cleared
                log_event("analyze_error", kind=type(e).__name__)
            return found, self._probe()

        self._run_bg(work, lambda result: self._done(job, s, *result))

    def _spin(self, job: _Job) -> None:
        if self._job is job:
            job.spinning = True
            self._c.refresh()

    def _done(self, job: _Job, s: FieldSnapshot, entry: _Entry | None, status: str) -> None:
        self._ai = status
        if self._job is job:
            self._job = None
            # The generation moves on every text change, focus loss, private field and pause.
            # Same generation: the focused field still holds the text that was analysed.
            if entry is not None and job.gen == self._c.generation:
                self._cache.put(job.key, entry)
                self._shown = (s, entry)
        self._c.refresh()

    # ------------------------------------------------------------------ internals

    def _probe(self) -> str:
        try:
            return self._supervisor.status()
        except Exception as e:  # noqa: BLE001 - a broken health check must not stop anything
            log_event("ai_status_error", kind=type(e).__name__)
            return self._ai

    def _resolve(self, s: FieldSnapshot) -> Resolved:
        r = self._resolver.resolve(s)
        profile = self._resolver.profile(self._overrides.get(r.key, ""))
        return r if profile is None else Resolved(profile, r.key, "override")

    def _key(self, s: FieldSnapshot, r: Resolved) -> str:
        p = r.profile
        return cache_key(s.text, p.name, p.target_tone, p.strictness, s.wm_class)

    def _entry_for(self, s: FieldSnapshot | None) -> _Entry | None:
        shown = self._shown
        if s is None or shown is None:
            return None
        of, entry = shown
        same = of is s or (of.field_id == s.field_id and of.text == s.text)
        return entry if same else None

    def _cancel_analysis(self) -> None:
        """Stop the analysis in flight: another one for the same text replaces it."""
        job, self._job = self._job, None
        if job is not None:
            job.cancelled = True
            self._supervisor.cancel_all()

    def _cancel(self, spare: _Rewrite | None = None) -> None:
        """Stop what is in flight and drop the preview: the text is no longer what the user
        has. Nothing is stopped while `spare` (a rewrite waiting for its field) is running:
        stopping is all or nothing, and the lanes keep the rest from piling up."""
        busy = False
        job, self._job = self._job, None
        if job is not None:
            job.cancelled = busy = True
        rw, self._rw = self._rw, None
        if rw is not None and rw.state == "loading":
            rw.cancelled = busy = True
        spec, self._spec = self._spec, None
        if spec is not None:
            spec.cancelled = busy = True
        if self._spec_timer is not None:
            self._spec_timer.cancel()
            self._spec_timer = None
        if busy and spare is None:
            self._supervisor.cancel_all()

    def _leave(self, keep_rewrite: bool = False) -> None:
        """No field, a private one, or paused: forget everything derived from the text.

        `keep_rewrite`: the focus only left the field. A rewrite in flight stays in flight
        for AWAIT_FIELD_MS in case the same field with the same text comes right back.
        """
        rw = self._rw
        if keep_rewrite and rw is not None and rw.state == "loading" and not rw.cancelled:
            self._rw = None
            self._drop_away()
            self._away = rw
            self._away_timer = self._clock.call_later(AWAIT_FIELD_MS, self._drop_away)
        elif not keep_rewrite:
            self._drop_away()
        self._cancel(spare=self._away)
        self._resolved = None
        self._shown = None
        self._seen = None
        self._made_for = None
        self._made = {}
        if not keep_rewrite:
            # After a mere blur the result of an apply just sent is still to come, and a
            # rewrite offered as a copy is still wanted when the same field is back.
            self._offer = None
            self._applied = None

    # ------------------------------------------------------------------ tone chips

    def tone_chips(self) -> tuple[ToneChip, ...]:
        """The chips of the focused field's profile (hotkeys 1-4), then "Rewrite as…"."""
        r = self._resolved
        if r is None:
            return ()
        chips = [ToneChip(key, TONES[key].label, n if n <= HOTKEY_CHIPS else None)
                 for n, key in enumerate(r.profile.chips, start=1) if key in TONES]
        return (*chips, ToneChip(CUSTOM, TONES[CUSTOM].label, None))

    def preview(self, tone: str, custom: str | None = None) -> None:
        """Show the rewrite of the focused field's text in `tone` (hover, click).

        A rewrite already made for this exact text is shown at once; otherwise one is
        started and streams into the panel. Only one rewrite is ever in flight: asking for
        another cancels the one before. `custom` is the instruction of the "custom" tone.
        """
        s = self._c.current
        target = self._target()
        if s is None or target is None or self._resolved is None:
            return
        if tone == CUSTOM:
            custom = (custom or "").strip()
            if not custom:
                return
        elif tone not in TONES:
            return
        else:
            custom = None
        key = _tone_key(tone, custom)
        rw = self._current_rewrite()
        if rw is not None and rw.key == key and rw.state != "failed":
            return  # already on show or on its way
        self._drop_rewrite()
        self._offer = None
        self._rw = rw = _Rewrite(target, tone, custom, s.text)
        made = self._made.get(key) if self._made_for == target else None
        if made is not None:
            self._show(rw, made)
        else:
            self._start(rw, s)
        self._c.refresh()

    def apply(self, tone: str | None = None, custom: str | None = None) -> None:
        """Type the rewrite over the whole field, through the verified write-back path.

        Without `tone`: the preview on show. With one (a hotkey): the rewrite in that tone,
        made first when there is none yet and applied as soon as it is complete. A rewrite
        that cannot be restored exactly is never typed: the panel offers it as a copy.
        """
        if tone is not None:
            self.preview(tone, custom)
        rw = self._current_rewrite()
        if rw is None:
            return
        if rw.state == "loading":
            rw.apply_when_done = True
        else:
            self._apply(rw)

    def apply_chip(self, n: int) -> None:
        """Hotkey N: apply the profile's Nth tone chip."""
        chip = next((c for c in self.tone_chips() if c.hotkey == n), None)
        if chip is not None:
            self.apply(chip.key)

    def choose(self, tone: str) -> None:
        """A click on a tone chip: show its rewrite first; a second click applies what is
        shown. "Rewrite as…" asks for the instruction."""
        if tone == CUSTOM:
            self.ask_custom()
            return
        rw = self._current_rewrite()
        if rw is not None and rw.key == tone and self._can_apply(rw):
            self._apply(rw)
        else:
            self.preview(tone)

    def ask_custom(self) -> None:
        """Ask how to rewrite. The entry takes the keyboard focus away from the field, so the
        answer waits until that exact text is the focused field again."""
        s = self._c.current
        if s is None or s.gated or self._overlay is None or self._resolved is None:
            return
        field_id, sha = s.field_id, text_sha256(s.text)

        def answered(text: str, focus_returned: bool) -> None:
            text = text.strip()
            if not text:
                return
            wait = AWAIT_FIELD_MS if focus_returned else AWAIT_COPY_MS
            self._awaited = _Awaited(field_id, sha, text, focus_returned,
                                     self._clock.now() + wait / 1000)
            self._resume_custom()

        self._overlay.ask_text(ASK_PROMPT, answered)

    def copy_rewrite(self) -> bool:
        """Put the rewrite that is offered as a copy on the clipboard; False without one."""
        s = self._c.current
        text = None if s is None else self._copy_text(s)
        if text is None or self._overlay is None:
            return False
        self._overlay.copy_to_clipboard(text)
        self._offer = None
        self._c.refresh()
        return True

    # ------------------------------------------------------------------ rewrite internals

    def _target(self) -> _Target | None:
        s = self._c.current
        if s is None or s.gated:
            return None
        return _Target(s.field_id, self._c.generation, text_sha256(s.text))

    def _current_rewrite(self) -> _Rewrite | None:
        """The preview, if it is for the text now in the focused field."""
        rw = self._rw
        if rw is None:
            return None
        s = self._c.current
        # Cheap checks first: this runs on every redraw.
        if (s is None or s.gated or rw.target.field_id != s.field_id
                or rw.target.gen != self._c.generation or rw.original != s.text):
            return None
        return rw

    def _can_apply(self, rw: _Rewrite) -> bool:
        o = rw.outcome
        return (rw.state == "ready" and o is not None and o.applicable
                and o.text is not None and not rw.copy_only)

    def _banner(self, rw: _Rewrite) -> str | None:
        o = rw.outcome
        if o is None:
            return None
        if o.reason is not None:
            return BANNERS.get(o.reason, BANNERS["failed"])
        if rw.copy_only:
            return COPY_ONLY
        if rw.tone == "sharpen_prompt" and o.text is not None:
            n = len(blanks(o.text))
            if n:
                return f"{n} blank(s) to fill in after applying"
        return None

    def _copy_text(self, s: FieldSnapshot) -> str | None:
        rw = self._current_rewrite()
        if (rw is not None and rw.state == "ready" and rw.outcome is not None
                and rw.outcome.text is not None and not self._can_apply(rw)):
            return rw.outcome.text
        if self._offer is not None and self._offer[0] == s.field_id:
            return self._offer[1]
        return None

    def _drop_rewrite(self) -> None:
        rw, self._rw = self._rw, None
        if rw is not None and rw.state == "loading":
            rw.cancelled = True
            self._supervisor.cancel_all()

    def _show(self, rw: _Rewrite, outcome: RewriteOutcome) -> None:
        rw.outcome = outcome
        rw.state = "ready" if outcome.reason in _KEEP else "failed"
        rw.shown = None if outcome.text is None else word_diff(rw.original, outcome.text)

    def _start(self, rw: _Rewrite, s: FieldSnapshot) -> None:
        r = self._resolved
        assert r is not None
        text, rich = s.text, bool(self._rich(s))

        def work() -> tuple[RewriteOutcome, str]:
            try:
                shielded = shield(text)  # only to show the stream: the path shields itself

                def on_delta(delta: str) -> None:
                    rw.partial += delta
                    now = self._clock.now()
                    if rw.last_render is None or (now - rw.last_render) * 1000 >= RENDER_MS:
                        rw.last_render = now
                        self._clock.post(lambda: self._render_partial(rw, shielded))

                ctx = self._resolver.ctx(s, r, mixed_language=bool(self._language(text)))
                outcome = rewrite_text(self._supervisor, text, rw.tone, ctx, rw.custom,
                                       on_delta, rich=rich)
            except Exception as e:  # noqa: BLE001 - the preview must always leave "loading"
                log_event("rewrite_error", kind=type(e).__name__)
                outcome = RewriteOutcome(None, False, "failed")
            return outcome, self._probe()

        self._run_bg(work, lambda result: self._rewrite_done(rw, *result))

    def _render_partial(self, rw: _Rewrite, shielded: Shielded) -> None:
        if self._current_rewrite() is rw and rw.state == "loading":
            rw.shown = word_diff(rw.original, partial_text(shielded, rw.partial))
            self._c.refresh()

    def _rewrite_done(self, rw: _Rewrite, outcome: RewriteOutcome, status: str) -> None:
        if self._away is rw and not rw.cancelled:
            rw.finished = (outcome, status)  # shown, or dropped, when a field is back
            self._ai = status
            self._c.refresh()
            return
        self._ai = status
        if rw.cancelled or self._rw is not rw:
            self._c.refresh()
            return
        if self._current_rewrite() is not rw or outcome.reason == "cancelled":
            # The text changed, the focus moved, or the call was stopped under us: a result
            # for a text the field no longer holds is never shown, kept or applied.
            self._rw = None
            self._c.refresh()
            return
        self._remember(rw.target, rw.key, outcome)
        self._show(rw, outcome)
        if rw.apply_when_done:
            self._apply(rw)
        self._c.refresh()

    def _remember(self, target: _Target, key: str, outcome: RewriteOutcome) -> None:
        if outcome.reason not in _KEEP:
            return
        if self._made_for != target:
            self._made_for, self._made = target, {}
        self._made[key] = outcome

    def _apply(self, rw: _Rewrite) -> None:
        o = rw.outcome
        if self._current_rewrite() is not rw or o is None:
            return
        if not self._can_apply(rw) or o.text is None:
            self._c.open_panel()  # where the reason and the copy are
            return
        # Pasted, not typed: a whole text typed in is many undo steps in some editors (one
        # per word in a GtkSourceView); pasted, one Ctrl+Z gives the user their text back.
        # Only where the app leaves the choice to Glance: a per-app `type`/`paste` wins.
        s = self._c.current
        method = "paste" if s is None or self._writeback(s) == "auto" else "auto"
        apply_id = self._c.request_apply(Edit(0, len(rw.original), o.text), method,
                                         expected_sha256=rw.target.sha)
        if apply_id:
            self._applied = (apply_id, rw.target.field_id, o.text)

    def _drop_away(self) -> None:
        """Give up on the rewrite whose field went out of sight."""
        rw, self._away = self._away, None
        if self._away_timer is not None:
            self._away_timer.cancel()
            self._away_timer = None
        if rw is not None:
            rw.cancelled = True
            newer = self._rw is not None and self._rw.state == "loading"
            if rw.finished is None and not newer:  # stopping is all or nothing
                self._supervisor.cancel_all()

    def _field_is_back(self) -> None:
        """A field is focused again (and the scheduler has counted it)."""
        rw, target = self._away, self._target()
        s = self._c.current
        if rw is not None and s is not None and target is not None:
            if (target.field_id, target.sha) != (rw.target.field_id, rw.target.sha) \
                    or s.text != rw.original or self._rw is not None:
                self._drop_away()  # another field, or other words: not what was asked about
            else:
                self._away = None
                if self._away_timer is not None:
                    self._away_timer.cancel()
                    self._away_timer = None
                rw.target = target  # the same text under the scheduler's new generation
                self._rw = rw
                if rw.finished is not None:
                    self._rewrite_done(rw, *rw.finished)
                else:
                    self._c.refresh()
        self._resume_custom()

    def _resume_custom(self) -> None:
        a = self._awaited
        s = self._c.current
        if a is None or s is None or s.gated:
            return  # the field is not back yet
        self._awaited = None
        if (s.field_id != a.field_id or text_sha256(s.text) != a.sha
                or self._clock.now() > a.until or self._resolved is None):
            return  # not the text that was asked about any more
        self._c.open_panel()
        if a.apply:
            self.apply(CUSTOM, a.custom)
            return
        self.preview(CUSTOM, a.custom)
        rw = self._current_rewrite()
        if rw is not None:
            rw.copy_only = True
            self._c.refresh()

    # ------------------------------------------------------------------ speculation

    def _arm_speculation(self) -> None:
        if self._spec_timer is not None:
            self._spec_timer.cancel()
            self._spec_timer = None
        target = self._target()
        if target is None or not self._supervisor.speculate():
            return
        idle_ms = (self._clock.now() - self._changed_at) * 1000
        self._spec_timer = self._clock.call_later(
            max(0, round(SPECULATE_IDLE_MS - idle_ms)), lambda: self._speculate(target))

    def _speculate(self, target: _Target) -> None:
        """Pre-generate the first chips, one after the other, in the speculative lane."""
        self._spec_timer = None
        s, r = self._c.current, self._resolved
        if s is None or r is None or self._target() != target or self._spec is not None:
            return
        made = self._made if self._made_for == target else {}
        tones = [t for t in r.profile.chips[:SPECULATE_CHIPS] if t in TONES and t not in made]
        if not tones:
            return
        self._spec = job = _Speculation(target)
        text, rich = s.text, bool(self._rich(s))

        def work() -> list[tuple[str, RewriteOutcome]]:
            out: list[tuple[str, RewriteOutcome]] = []
            try:
                if not (self._supervisor.speculate() and self._supervisor.auto_allowed()):
                    return out
                ctx = self._resolver.ctx(s, r, mixed_language=bool(self._language(text)))
                for tone in tones:
                    if job.cancelled:
                        break
                    outcome = rewrite_text(self._supervisor, text, tone, ctx, rich=rich,
                                           speculative=True)
                    if outcome.reason not in _KEEP:
                        break  # stopped, out of budget or failing: not worth the next one
                    out.append((tone, outcome))
            except Exception as e:  # noqa: BLE001
                log_event("speculate_error", kind=type(e).__name__)
            return out

        self._run_bg(work, lambda results: self._speculated(job, results))

    def _speculated(self, job: _Speculation, results: list[tuple[str, RewriteOutcome]]) -> None:
        if self._spec is job:
            self._spec = None
        if job.cancelled or self._target() != job.target:
            return
        for tone, outcome in results:
            self._remember(job.target, tone, outcome)
