"""WriteBackEngine: put one edit into the focused field, check it landed, undo it if not.

The rule everything here serves: never leave the user's text different from both what they
had and what they asked for. So nothing is synthesized unless the field still holds exactly
the text the edit was computed for, every write is re-read, and a write that did not come
out as intended is taken back with the application's own undo.

Nor may a write interleave with the user's own keys: no apply starts within QUIET_S of their
last change to the text (`TypingWatch`; it waits up to PAUSE_WAIT_S for that pause, then
gives up), typing stops between key groups when they type or press a key, and whatever the
outcome the caret goes back to where they were typing.

Text is changed only through the keyboard (typing, or one paste); AT-SPI is used to read,
to select and to place the caret, never to edit (findings S4.7).
"""
from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from glance.capture.ports import A11yField, A11yTimeout, FieldRead
from glance.config import AppCfg
from glance.grammar.issues import apply_edit
from glance.log import log_event
from glance.privacy.entities import find_protected, overlaps
from glance.privacy.gate import FieldMeta
from glance.protocol import ApplyRequest, ApplyResult, Edit, text_sha256
from glance.writeback.activity import QUIET_S, TypingWatch
from glance.writeback.ports import ClipboardPort, ClipSnapshot, KeyPort

KEYS_RELEASED_TIMEOUT_MS = 1500  # S4.8
SETTLE_S = 0.03       # after an AT-SPI selection or caret move, before it is used (S4.1)
PASTE_READ_S = 0.05   # after Ctrl+V, before the clipboard goes back
VERIFY_S = 0.15       # after a write or an undo, before the re-read
LATE_S = 0.3          # a second look before a write is declared not to have landed
MAX_UNDO_STEPS = 4
TYPE_GROUP = 8        # characters typed between two checks for the user's own keys
PAUSE_WAIT_S = 0.6    # how long an apply waits for the user to pause before it gives up
PASTE_ID = "paste"  # the apply id of an unverified paste

COPY_INSTEAD = "use Copy instead"
_NOT_RESPONDING = "target not responding"
_RELEASE_KEYS = "release modifier keys"
_CLIP_LOST = "clipboard could not be restored"
TYPING = "you are typing; try again when you pause"
_KEY_DOWN = "a key is down"
_TYPED_OVER = "you typed while Glance was writing"

# What applications do to text on their own and what must not count as a failed write:
# no-break spaces for spaces (contenteditable), typographic quotes (Slack, S4.3).
_FOLD = str.maketrans({
    "\u00a0": " ", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
})


def _why(detail: str) -> str:
    """The engine's own reason (a fixed phrase, never the user's text) as a log token."""
    head = detail.split(";")[0].strip().replace(" ", "_")
    return "".join(ch for ch in head if ch.isalnum() or ch == "_")[:48]


def normalize(text: str) -> str:
    """The form in which the text after a write is compared with the text intended."""
    text = unicodedata.normalize("NFC", text.translate(_FOLD))
    return text.removesuffix("\n")


def _keeps_protected(text: str, edit: Edit) -> bool:
    """True if the edit leaves every protected entity of `text` as it is.

    An entity the edit range touches must lie wholly inside the range and appear verbatim in
    the replacement, in the original order. An embedded object (U+FFFC: an emoji image, a
    link) can never be retyped, so a range that holds one is never acceptable.
    """
    spans = find_protected(text)
    if not overlaps(spans, edit.start, edit.end):
        return True
    pos = 0
    for span in spans:
        if not (span.start < edit.end and edit.start < span.end):
            continue
        if span.kind == "object" or span.start < edit.start or span.end > edit.end:
            return False
        found = edit.replacement.find(text[span.start:span.end], pos)
        if found < 0:
            return False
        pos = found + span.end - span.start
    return True


def _typeable_chars(text: str) -> bool:
    """No control characters: a typed Return sends the message in a chat application."""
    return not any(unicodedata.category(ch) == "Cc" for ch in text)


def _one_left_per_char(text: str) -> bool:
    """True if Shift+Left pressed len(text) times selects exactly `text`.

    Editors move the caret by cluster: over a combining mark, a joiner or a surrogate pair
    one Left covers more than one code point, and the selection would run past the start.
    """
    return not any(
        ord(ch) > 0xFFFF or unicodedata.category(ch) in ("Mn", "Mc", "Me", "Cf", "Cs")
        for ch in text
    )


def _mid_write(text: str, old: str, edit: Edit) -> bool:
    """True if `text` is a state this write passes through on its way from `old`.

    That is `old` with the edit range replaced by a proper prefix of the replacement:
    nothing yet (the range deleted), or its first characters. A GtkSourceView undoes a typed
    replace in two steps and stops on "first character typed" in between (S4.4).
    """
    head, tail = old[: edit.start], old[edit.end:]
    if len(text) < len(head) + len(tail):
        return False
    if not text.startswith(head) or not text.endswith(tail):
        return False
    middle = text[len(head): len(text) - len(tail)]
    return len(middle) < len(edit.replacement) and edit.replacement.startswith(middle)


def _follow(offset: int, old: str, new: str) -> int:
    """Where `offset` into `old` is in `new`, for a change in one place.

    Before the change it stays, after it it moves by the change in length; inside it, it
    goes to the end of what changed.
    """
    n = min(len(old), len(new))
    head = 0
    while head < n and old[head] == new[head]:
        head += 1
    tail = 0
    while tail < n - head and old[len(old) - 1 - tail] == new[len(new) - 1 - tail]:
        tail += 1
    if offset <= head:
        return offset
    if offset >= len(old) - tail:
        return offset + len(new) - len(old)
    return len(new) - tail


def _passes_through(old: str, edit: Edit) -> list[str]:
    """Every text this write can leave on its way, and the one it leaves when undone."""
    head, tail = old[: edit.start], old[edit.end:]
    return [old] + [head + edit.replacement[:k] + tail for k in range(len(edit.replacement) + 1)]


def _ours(text: str, old: str, edit: Edit) -> bool:
    """True if `text` is a state this write alone can have left (see `_passes_through`)."""
    return normalize(text) in {normalize(t) for t in _passes_through(old, edit)}


class _Stop(Exception):
    """Ends an apply with the given result; raised only where no cleanup is pending."""

    def __init__(self, status: str, detail: str) -> None:
        super().__init__(status)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class _LastApply:
    apply_id: str
    field_id: str
    window: int      # the X window that was pinned for the write
    before: str
    after: str       # the text as the application reported it after the write
    edit: Edit
    undo_steps: int


class WriteBackEngine:
    """Applies edits to the focused field.

    `field` returns the focused field, `app_cfg` the settings for a window class, `sleep`
    blocks for the given seconds. `allowed`, when given, is the privacy gate: a field whose
    metadata it rejects is refused before its text is read. `watch`, when given, says when
    the user last typed; the engine tells it which texts its own writes produce.
    """

    def __init__(
        self,
        field: Callable[[], A11yField | None],
        keys: KeyPort,
        clip: ClipboardPort,
        app_cfg: Callable[[str], AppCfg],
        sleep: Callable[[float], None],
        allowed: Callable[[FieldMeta], bool] | None = None,
        watch: TypingWatch | None = None,
    ) -> None:
        self._field = field
        self._watch = watch
        self._keys = keys
        self._clip = clip
        self._app_cfg = app_cfg
        self._sleep = sleep
        self._allowed = allowed
        self._last: _LastApply | None = None
        self._clip_lost = False

    # ------------------------------------------------------------------ apply

    def apply(self, req: ApplyRequest) -> ApplyResult:
        self._clip_lost = False
        try:
            status, detail = self._apply(req)
        except _Stop as stop:
            status, detail = stop.status, stop.detail
        except A11yTimeout:
            status, detail = "failed", _NOT_RESPONDING
        if self._clip_lost:
            # Said whatever else happened: the user's clipboard is not what it was.
            detail = f"{detail}; {_CLIP_LOST}" if detail else _CLIP_LOST
        log_event("writeback_apply", status=status, chars=len(req.edit.replacement),
                  clipboard_lost=self._clip_lost, why=_why(detail))
        return ApplyResult(req.apply_id, status, detail)

    def _apply(self, req: ApplyRequest) -> tuple[str, str]:
        edit = req.edit
        field = self._current(req.field_id)
        if field is None:
            return "stale", "focus moved"
        meta = field.meta()
        # Pin the window: every key from here on is sent only while this very window has
        # the keyboard. The pid ties it to the field; the window id is what is compared
        # later, so another window of the same application does not pass.
        window, pid = self._keys.focus_target()
        if not window or pid != meta.pid:
            return "stale", "focus moved"
        if self._allowed is not None and not self._allowed(meta):
            return "refused", "private field"
        cfg = self._app_cfg(meta.wm_class)
        if cfg.writeback not in ("auto", "type", "paste"):
            return "refused", "writeback off"
        if not self._wait_for_pause():
            return "stale", TYPING
        before = field.read()  # where the user's caret is goes back there in the end
        old = before.text
        if text_sha256(old) != req.expected_sha256:
            return "stale", "text changed"
        # Nothing has been synthesized and the clipboard is untouched up to here.
        if not 0 <= edit.start <= edit.end <= len(old):
            return "refused", "bad range"
        if not _keeps_protected(old, edit):
            return "refused", "protected"
        expected = apply_edit(old, edit)
        if expected == old:
            return "verified", "no change"
        how = self._method(req.method, cfg, edit.replacement)
        if how is None:
            return "refused", "cannot type this text"
        snap = None
        if how == "paste":
            # Before anything is selected, so that a refusal leaves the field as it was.
            snap = self._clip.save()
            if snap is None:
                return "refused", "clipboard not restorable"
        if not self._keys.wait_modifiers_released(KEYS_RELEASED_TIMEOUT_MS):
            return "refused", _RELEASE_KEYS
        # Saving the clipboard and waiting for the keys took time: look again.
        field = self._current(req.field_id, window)
        if field is None:
            return "stale", "focus moved"
        if self._user_typing():
            return "stale", TYPING
        if text_sha256(field.read().text) != req.expected_sha256:
            return "stale", "text changed"

        if self._watch is not None:
            self._watch.expect(_passes_through(old, edit))
        try:
            self._select(field, cfg, old, edit, req.expected_sha256, window)
        except _Stop:
            self._put_back(field, before)
            raise
        if self._current(req.field_id, window) is None:
            # Selecting took a moment and the focus went elsewhere: keys would follow it.
            self._put_back(field, before)
            return "stale", "focus moved"
        if self._keys.any_key_down() or self._user_typing():
            # The user pressed something since the wait: with a modifier down the text
            # would arrive as shortcuts, and their key would replace the selection.
            self._put_back(field, before)
            return "stale", _KEY_DOWN if self._keys.any_key_down() else TYPING
        self._last = None  # the text changes now; an earlier apply cannot be reverted any more
        interrupted = False
        try:
            interrupted = self._insert(how, field, old, edit, cfg, snap, window)
        except _Stop:
            self._put_back(field, before)  # nothing was sent; only the selection is ours
            raise
        except Exception as e:  # noqa: BLE001 - KeysAborted, or an adapter that broke
            # Any part of the text may be in the field. Whatever happened, the field is read
            # again below and put back if it does not hold what was asked for.
            log_event("writeback_insert_error", kind=type(e).__name__)

        self._sleep(VERIFY_S)
        text = field.read().text
        if text == old:
            # Nothing yet. A slow application may still be working on it, and "did not
            # land" must not be said about a change that then appears.
            self._sleep(LATE_S)
            text = field.read().text
        if not interrupted and (
                text == expected or (text != old and normalize(text) == normalize(expected))):
            self._last = _LastApply(
                req.apply_id, req.field_id, window, before=old, after=text, edit=edit,
                undo_steps=cfg.undo_steps)
            if self._watch is not None:
                self._watch.expect([text])
            self._keep_caret(field, before, edit, text)
            return "verified", ""
        if self._current(req.field_id, window) is None:
            # The keys, or some of them, went to whatever has the focus now, and so would
            # a Ctrl+Z. All that can still be done is not to leave a selection behind.
            self._put_back(field, before)
            return "failed", f"focus moved; {COPY_INSTEAD}"
        if text == old:
            # Nothing arrived (a swallowed Ctrl+V, say). Ctrl+Z would undo the user's own
            # last edit; only the selection is ours, and it must not eat their next key.
            self._put_back(field, before)
            if interrupted:
                return "stale", TYPING
            return "reverted", f"the change did not land; {COPY_INSTEAD}"
        if (interrupted or self._user_typing()) and not _ours(text, old, edit):
            # The user's own keys are in the field: a Ctrl+Z would take those first.
            self._put_back(field, before)
            return "failed", f"{_TYPED_OVER}; {COPY_INSTEAD}"
        undone = self._undo(field, window, old, edit, cfg.undo_steps)
        self._put_back(field, before)
        if undone:
            if interrupted:
                return "stale", TYPING  # taken back whole: nothing to copy, just try again
            return "reverted", f"the change did not apply cleanly; {COPY_INSTEAD}"
        return "failed", f"could not restore; {COPY_INSTEAD}"

    def _insert(
        self, how: str, field: A11yField, old: str, edit: Edit, cfg: AppCfg,
        snap: ClipSnapshot | None, window: int,
    ) -> bool:
        """Write the replacement over the selection; True if typing stopped part-way
        because the user typed or pressed a key."""
        replacement = edit.replacement
        if how == "type":
            return self._type(field, old, edit, cfg, window)
        if how == "delete":
            self._keys.chord("BackSpace")
            return False
        self._paste_over(replacement, snap, window)
        return False

    def _type(self, field: A11yField, old: str, edit: Edit, cfg: AppCfg, window: int) -> bool:
        """Type in groups of TYPE_GROUP; between two groups the user's key, their typing, or
        a text that is not one of this write's own states ends it (the rest is never sent).
        An application still behind with the last group is no reason to stop."""
        rep = edit.replacement
        for i in range(0, len(rep), TYPE_GROUP):
            if i:
                if self._keys.any_key_down() or self._user_typing():
                    return True
                if not _ours(field.read().text, old, edit):
                    return False  # not ours: the re-read after the write judges it
            self._keys.type_text(rep[i:i + TYPE_GROUP], cfg.type_delay_ms, window)
        return False

    def _restore_clipboard(self, snap: ClipSnapshot) -> None:
        try:
            self._clip.restore(snap)
        except Exception as e:  # noqa: BLE001 - reported in the result
            self._clip_lost = True
            log_event("writeback_clipboard_error", kind=type(e).__name__)

    def _paste_over(self, replacement: str, snap: ClipSnapshot | None, window: int) -> None:
        """Offer `replacement` to `window` alone, Ctrl+V, and put the clipboard back."""
        assert snap is not None
        try:
            self._clip.set_secret_text(replacement, window)
            if not self._on_target(window):  # taking the clipboard took a moment
                raise _Stop("stale", "focus moved")
            if self._keys.any_key_down():
                raise _Stop("stale", _KEY_DOWN)
            if self._user_typing():
                raise _Stop("stale", TYPING)
            self._keys.chord("ctrl", "v")
            self._sleep(PASTE_READ_S)
        finally:
            self._restore_clipboard(snap)

    def _wait_for_pause(self) -> bool:
        """Wait until the user has not changed the text for QUIET_S; False if they are
        still at it after PAUSE_WAIT_S. Nothing has been sent or selected yet."""
        waited = 0.0
        while self._user_typing():
            if waited >= PAUSE_WAIT_S:
                return False
            step = min(QUIET_S - self._watch.quiet_s(), PAUSE_WAIT_S - waited) \
                if self._watch is not None else 0.0
            step = max(step, 0.02)
            self._sleep(step)
            waited += step
        return True

    def _user_typing(self) -> bool:
        """True if the user changed the text less than QUIET_S ago."""
        if self._watch is None:
            return False
        self._field()  # in capture this first dispatches what the main loop has pending
        return self._watch.quiet_s() < QUIET_S

    def _put_back(self, field: A11yField, before: FieldRead) -> None:
        """Return the caret, or the user's own selection, to where the user had it.

        Their next key must land where they were typing, not where this write selected.
        A text that changed meanwhile moves the spot with it (`_follow`).
        """
        try:
            text = field.read().text
            sel = before.selection
            if sel is not None and sel[0] != sel[1] and text == before.text \
                    and field.set_selection(*sel):
                return
            field.set_caret(_follow(before.caret, before.text, text))
        except A11yTimeout:
            pass

    def _keep_caret(self, field: A11yField, before: FieldRead, edit: Edit, text: str) -> None:
        """After a verified write: the caret back where the user was, if that is not where
        the write left it (the end of the replacement)."""
        sel = before.selection
        if sel is not None and sel[0] != sel[1]:
            return
        want = _follow(before.caret, before.text, text)
        if want != edit.end + len(text) - len(before.text):
            self._collapse(field, want)

    def _on_target(self, window: int) -> bool:
        """True if the pinned window still has the keyboard, as the X server sees it."""
        return bool(window) and self._keys.focus_target()[0] == window

    @staticmethod
    def _collapse(field: A11yField, offset: int) -> None:
        """Drop a selection this engine made; a field that no longer answers keeps it."""
        try:
            field.set_caret(offset)
        except A11yTimeout:
            pass

    def _current(self, field_id: str, window: int | None = None) -> A11yField | None:
        """The focused field if it is still the one named by `field_id`.

        With `window`, the X server must also say that this window has the keyboard.
        Synthetic keys go there whatever the accessibility events have reported so far, so
        it is asked directly before any key is sent.
        """
        field = self._field()
        if field is None or field.key != field_id:
            return None
        if window is not None and not self._on_target(window):
            return None
        return field

    def _method(self, requested: str, cfg: AppCfg, replacement: str) -> str | None:
        """`type`, `paste` or `delete`; None when typing was demanded and is not possible."""
        if replacement == "":
            return "delete"
        if requested == "auto":
            requested = cfg.writeback  # a per-app `type` or `paste` decides for `auto`
        if requested == "paste":
            return "paste"
        typeable = _typeable_chars(replacement) and self._keys.can_type(replacement)
        if requested == "type":
            return "type" if typeable else None
        return "type" if typeable and len(replacement) <= cfg.type_max_chars else "paste"

    def _select(
        self, field: A11yField, cfg: AppCfg, old: str, edit: Edit, sha: str, window: int,
    ) -> None:
        """Select `[start, end)`, or place the caret for an insertion. Raises `_Stop`."""
        start, end = edit.start, edit.end
        if start == end:
            if not field.set_caret(start) and field.read().caret != start:
                raise _Stop("refused", "cannot select the range")
            self._sleep(SETTLE_S)
            return
        by_atspi = cfg.select != "keys" and field.set_selection(start, end)
        if by_atspi:
            self._sleep(SETTLE_S)
            read = field.read()
            if text_sha256(read.text) != sha:
                # The user typed after all; whatever was selected is theirs to deal with,
                # and moving their caret now would only make it worse.
                raise _Stop("stale", "text changed")
            if read.selection == (start, end):
                return
        if not _one_left_per_char(old[start:end]):
            if by_atspi:
                field.set_caret(end)  # whatever did get selected must not stay selected
            raise _Stop("refused", "cannot select the range")
        if not field.set_caret(end):
            raise _Stop("refused", "cannot select the range")
        self._sleep(SETTLE_S)
        for _ in range(end - start):
            # Each press is a key of its own: none may go out once the window has changed.
            if not self._on_target(window):
                raise _Stop("stale", "focus moved")
            self._keys.chord("shift", "Left")
        # No read-back here: after a long key selection the application reports it late
        # (S4.1). The re-read after the write is the check.
        self._sleep(SETTLE_S)

    def _undo(
        self, field: A11yField, window: int, original: str, edit: Edit, steps: int,
    ) -> bool:
        """Press Ctrl+Z until the field holds `original`; True if it does.

        At most `steps` presses, re-reading after each so that no press is sent once the
        original is back (one more would undo the user's own last edit). Where the count is
        configured too low, further presses are sent only while the field holds a state
        that only this write can have produced (`_mid_write`), up to MAX_UNDO_STEPS.
        """
        limit = max(1, min(steps, MAX_UNDO_STEPS))
        sent = 0
        while sent < limit:
            if self._current(field.key, window) is None:
                return False
            self._keys.chord("ctrl", "z")
            sent += 1
            self._sleep(VERIFY_S)
            text = field.read().text
            if text == original:
                return True
            if sent == limit and limit < MAX_UNDO_STEPS and _mid_write(text, original, edit):
                limit += 1
        return False

    # ------------------------------------------------------------------ unverified paste

    def paste_unverified(self, text: str, window: int = 0) -> ApplyResult:
        """Paste `text` once into the window that has the keyboard; nothing is read back.

        For applications whose field is not captured (select-anything). The window is pinned
        the way `apply` pins it: `window`, when given, must be the one that has the keyboard,
        and no key is sent once another window has it. The clipboard is saved first (refused
        if it cannot be restored) and always put back. The result is `unverified`: Glance
        cannot see the text, so it cannot undo it either, and `revert_last` knows nothing of
        it. Callers never use this for a terminal.
        """
        self._clip_lost = False
        self._last = None
        try:
            status, detail = self._paste(text, window)
        except _Stop as stop:
            status, detail = stop.status, stop.detail
        if self._clip_lost:
            detail = f"{detail}; {_CLIP_LOST}" if detail else _CLIP_LOST
        log_event("writeback_paste", status=status, chars=len(text),
                  clipboard_lost=self._clip_lost)
        return ApplyResult(PASTE_ID, status, detail)

    def _paste(self, text: str, window: int) -> tuple[str, str]:
        target, _pid = self._keys.focus_target()
        if not target or (window and target != window):
            return "stale", "focus moved"
        snap = self._clip.save()
        if snap is None:
            return "refused", "clipboard not restorable"
        if not self._keys.wait_modifiers_released(KEYS_RELEASED_TIMEOUT_MS):
            return "refused", _RELEASE_KEYS
        if not self._on_target(target):
            return "stale", "focus moved"
        if self._keys.any_key_down():
            return "stale", "a key is down"
        self._paste_over(text, snap, target)
        return "unverified", ""

    # ------------------------------------------------------------------ revert

    def revert_last(self) -> ApplyResult:
        """Undo the last verified apply, if the field still holds exactly its result."""
        last = self._last
        if last is None:
            return ApplyResult("", "stale", "nothing to revert")
        try:
            status, detail = self._revert(last)
        except A11yTimeout:
            self._last = None
            status, detail = "failed", _NOT_RESPONDING
        log_event("writeback_revert", status=status)
        return ApplyResult(last.apply_id, status, detail)

    def _revert(self, last: _LastApply) -> tuple[str, str]:
        if not self._holds_result(last):
            self._last = None
            return "stale", "text changed"
        if not self._keys.wait_modifiers_released(KEYS_RELEASED_TIMEOUT_MS):
            return "refused", _RELEASE_KEYS  # nothing was done: the revert stays available
        self._last = None
        if not self._holds_result(last):
            return "stale", "text changed"
        field = self._current(last.field_id, last.window)
        if field is not None and self._undo(
                field, last.window, last.before, last.edit, last.undo_steps):
            return "reverted", ""
        return "failed", f"could not restore; {COPY_INSTEAD}"

    def _holds_result(self, last: _LastApply) -> bool:
        field = self._current(last.field_id, last.window)
        return field is not None and text_sha256(field.read().text) == text_sha256(last.after)
