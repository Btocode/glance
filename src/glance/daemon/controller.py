"""The daemon's brain: snapshots in, chip / panel / apply requests out. No GTK.

Every method is called on the main loop. Later plans extend the controller only through the
hook lists and `add_listener` below: they append, they never replace.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from glance.clock import Clock, Handle
from glance.config import Config
from glance.daemon.scheduler import CheckScheduler
from glance.grammar.corrections import combine_sources
from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue, issue_edit, merge_edits
from glance.log import log_event
from glance.privacy.entities import find_protected, overlaps
from glance.protocol import ApplyRequest, ApplyResult, Edit, FieldSnapshot, text_sha256
from glance.ui.chip_model import ChipInputs, ChipState, ChipView, chip_view
from glance.ui.icon_toolbar import IconToolbar, icon_toolbar, may_open
from glance.ui.panel_model import PanelView, build_panel
from glance.ui.placement import Anchor, place
from glance.ui.ports import CapturePort, GrammarPort, NotifierPort, OverlayPort, TrayPort
from glance.ui.status_model import (
    CHECKING_LINE,
    NOTHING_LINE,
    StatusInputs,
    StatusState,
    StatusView,
    status_view,
)
from glance.ui.strip_model import StripView, strip_fix, strip_view
from glance.wordlimit import LOCAL_MAX_WORDS, word_window

T = TypeVar("T")

FIX_INDIVIDUALLY = "Fix individually — text between fixes is protected"
REVERTED = ("Glance undid a change", "The app changed the text differently. Nothing was lost.")
APPLY_FAILED = ("Glance could not apply the change", "Open the panel to copy it instead.")
# How long capture gets to answer an apply. Its own budget (key release wait, typing, the
# verifying re-read, undo) stays well inside this.
APPLY_TIMEOUT_MS = 5000
REVERT_FAILED = ("Glance could not undo the change", "Use the app's own undo instead.")
# Alt+Enter on the strip is a key grab: its press can blur the app before the press itself is
# seen here (X sends the app a NotifyGrab focus change). A blur this recent is that one: the
# fix the strip showed is applied when the same field, same text, is back within the grace.
USE_BLUR_S = 0.5
USE_GRACE_S = 2.0
# `[general] check = "shortcut"`: after the check hotkey the strip opens once the local check
# is back and the AI analysis too, or after this long with what there is (the AI's findings
# then join the open strip).
OPEN_WAIT_MS = 4000


@dataclass(frozen=True)
class _Shown:
    """The fix the strip is (or was, until a blur) showing: what "Use" applies."""

    field_id: str
    sha: str
    edit: Edit


@dataclass(frozen=True)
class _Pending:
    apply_id: str
    field_id: str
    sha: str  # hash of the text the edit was made for
    replacement: str


class Controller:
    """Owns the current field, its issues and what is on screen.

    Extension points (all optional, all additive):
      scheduler          the CheckScheduler; `generation` mirrors its counter
      current            the latest snapshot, None without a focused field
      issues             the filtered issues for `current`, sorted by position
      on_analyze_due     called with (latest snapshot, generation) when an analysis is due
      extra_chip_inputs  providers of ChipInputs fields, merged in order (later wins)
      extra_panel        transforms of the PanelView, applied in order
      extra_strip        transforms of the StripView (the collapsed suggestion), in order
      extra_issues       providers of more issues for a snapshot (dropped where they overlap
                         a local one)
      extra_ignored      providers of rule names to ignore for a snapshot
      extra_corrects     predicates: True when another source's corrections replace the
                         local checker's for a snapshot (the AI's corrected text)
      requested_gen      the generation of the last check the user asked for (check_now)
      extra_status       providers of extra `status()` entries (numbers, bools, short enums)
      extra_use          tried in order before `use_strip()` applies the corrected text; one
                         that returns True has applied something else (a selected tone)
      extra_preview      providers of a strip in place of the corrected text while a preview
                         is open (`open_preview()`: a tone the icon's toolbar asked for);
                         None: nothing to show any more (the preview closes)
      add_listener(obj)  obj may define on_snapshot(s), on_typing(), on_focus_lost(reason),
                         on_apply_result(r), on_pause_changed(paused), on_capture_lost(),
                         on_reset()
      refresh()          recompute the issues and redraw; call after a provider's data changed
      request_apply()    send an edit through the verified write-back path
      use_strip()        apply what the strip shows (its "Use" button, Alt+Enter)
      toggle_strip()     the status icon's click: open the strip, or close it again
      check_now()        the check hotkey: check the field now and open the strip

    `[general] check` picks when the text is checked. "typing": as the user types (the
    scheduler's timers). "shortcut" (the default): never by itself; `check_now()` (the
    check hotkey, a click on the icon) checks the text as it is and opens the strip with the
    result. An edit after that makes the result stale: the strip closes and the icon goes
    back to its quiet "ready to check" state until the next check.

    The status icon is shown for every focused writable field (`status_icon()`); the strip
    shows by itself only with `[ui] suggestions = "auto"`, else once the user opens it (the
    icon, the panel hotkey) until Esc, another click, a focus change or an apply.
    Providers are never called for a gated field or while paused. A provider or listener that
    raises is logged and skipped.

    Listener contract: `on_snapshot` is only ever called with an ungated snapshot and never
    while paused. A field that turns private is reported once as `on_focus_lost("gated")`.
    Pausing is reported by `on_pause_changed(True)`; on resume, `on_pause_changed(False)` is
    followed by `on_snapshot` with the current snapshot when there is an ungated one.
    `on_apply_result` gets every result capture sends, including late and unmatched ones.
    """

    def __init__(
        self,
        clock: Clock,
        cfg: Callable[[], Config],
        grammar: GrammarPort,
        issue_filter: IssueFilter,
        overlay: OverlayPort,
        capture: CapturePort,
        notifier: NotifierPort,
        tray: TrayPort,
        run_bg: Callable[[Callable[[], T], Callable[[T], None]], None],
    ) -> None:
        self._clock = clock
        self._cfg = cfg
        self._grammar = grammar
        self._filter = issue_filter
        self._overlay = overlay
        self._capture = capture
        self._notifier = notifier
        self._tray = tray
        self._run_bg = run_bg
        self.scheduler = CheckScheduler(
            clock, lambda: cfg().general, on_local=self._on_local,
            on_settled=self._on_settled, on_analyze=self._on_analyze, on_typing=self._on_typing)

        self.current: FieldSnapshot | None = None
        self.on_analyze_due: Callable[[FieldSnapshot, int], None] | None = None
        self.extra_chip_inputs: list[Callable[[FieldSnapshot], dict[str, Any]]] = []
        self.extra_panel: list[Callable[[FieldSnapshot, PanelView], PanelView]] = []
        self.extra_strip: list[Callable[[FieldSnapshot, StripView], StripView]] = []
        self.extra_issues: list[Callable[[FieldSnapshot], list[Issue]]] = []
        self.extra_ignored: list[Callable[[FieldSnapshot], frozenset[str]]] = []
        self.extra_corrects: list[Callable[[FieldSnapshot], bool]] = []
        # True: the local checker's findings are not shown for this text (the AI's check is
        # coming, `[general] checker = "ai"`); they come back when no AI answers.
        self.extra_local_hidden: list[Callable[[FieldSnapshot], bool]] = []
        self.requested_gen: int | None = None
        self.extra_status: list[Callable[[], dict[str, Any]]] = []
        self.extra_use: list[Callable[[], bool]] = []
        self.extra_preview: list[Callable[[FieldSnapshot], StripView | None]] = []
        self.icon_tones: Callable[[], tuple[str, ...]] = tuple  # the profile's tones
        self._listeners: list[object] = []

        self._local: list[Issue] = []  # the grammar port's issues for the current text
        self._issues: list[Issue] = []
        self._awaiting_text = False  # an apply was verified; its new text has not arrived yet
        self._typing = False
        self._settled = False
        self._snoozed_field: str | None = None
        self._panel_open = False
        self._preview_open = False  # the strip shows `extra_preview`'s view (a tone)
        self._banner: str | None = None
        self._copy_offer: str | None = None
        self._capture_lost = False
        self._paused = False
        self._pause_until: float | None = None
        self._pause_timer: Handle | None = None
        self._pending: _Pending | None = None
        self._pending_timer: Handle | None = None
        self._last_verified = ""  # id of the apply a revert would undo
        self._revert_of: str | None = None  # a revert is in flight for this apply id
        self._apply_seq = 0
        self._tray_state: str | None = None
        self._strip_shown: _Shown | None = None
        self._blurred_at: float | None = None
        self._use_back: tuple[_Shown, float] | None = None  # (fix, until): waits for its field
        self._locked = False  # the screen is locked: nothing is shown or processed
        self._strip_open = False  # the user opened the strip (icon click, panel hotkey)
        self._strip_visible = False  # a strip is on screen now
        self._checking = False  # the text changed; its local check has not come back yet
        self._lock_paused = False  # ...and the lock, not the user, paused us
        self._checked: tuple[str, str] | None = None  # (field id, text) checked on request
        self._open_when_checked = False  # check_now: open the strip once the check is back
        self._open_by = 0.0  # ...or by this time, with what there is
        self._open_timer: Handle | None = None
        self._mode = cfg().general.check
        self._open_panel_too = False  # the drawer opens with the strip once checked
        self._typed_at: float | None = None  # the last time the field's text changed
        self._checked_at: float | None = None  # when check_now last ran
        self._blurred_field: str | None = None  # the field a blur took away
        # (field id, until, with drawer): the check hotkey's own grab blurred this field;
        # check it once it is back
        self._check_back: tuple[str, float, bool] | None = None
        # (field id, until): the panel was open when a hotkey's key grab blurred the field
        self._panel_back: tuple[str, float] | None = None

    # ------------------------------------------------------------------ read-only state

    @property
    def generation(self) -> int:
        return self.scheduler.generation

    @property
    def checks_on_request(self) -> bool:
        """`[general] check = "shortcut"`: nothing is checked or asked of an AI while the
        user types (features that would run by themselves stay quiet too)."""
        return self._cfg().general.check == "shortcut"

    @property
    def panel_open(self) -> bool:
        """The field's panel (the drawer) is open."""
        return self._panel_open

    @property
    def issues(self) -> tuple[Issue, ...]:
        return tuple(self._issues)

    def status(self) -> dict[str, Any]:
        """A text-free summary for `glance status`."""
        s = self.current
        remaining = None
        if self._paused and self._pause_until is not None:
            remaining = max(0, math.ceil(self._pause_until - self._clock.now()))
        out: dict[str, Any] = {
            "paused": self._paused,
            "pause_remaining_s": remaining,
            "field": None if s is None else
            {"wm_class": s.wm_class, "gated": s.gated, "len": len(s.text)},
            "local_issues": sum(1 for i in self._issues if i.source == "harper"),
            "capture_connected": self._connected(),
            "harper_alive": bool(self._grammar.alive),
        }
        for provider in self.extra_status:
            out.update(self._call("extra_status", provider, default={}))
        return out

    def chip(self) -> ChipView:
        """What the chip shows for the current field right now."""
        return chip_view(self._chip_inputs(self.current), self._cfg().general.min_chars_chip)

    def strip(self) -> StripView | None:
        """What the suggestion strip shows for the current field right now (None: hidden)."""
        s = self.current
        if s is None:
            return None
        inputs = self._chip_inputs(s)
        content = self._content(s, inputs)
        return self._visible_strip(s, content, self._status(s, inputs, content))

    @property
    def preview_open(self) -> bool:
        """The strip shows a preview (a tone the icon's toolbar asked for)."""
        return self._preview_open

    def icon_toolbar(self, check_chord: str = "", typing_ok: bool = False) -> IconToolbar | None:
        """What the status icon's toolbar offers now; None when there is no icon, or while
        the user is typing (a key within the last moment) unless `typing_ok` (a right-click
        asks for it on purpose)."""
        s = self.current
        if s is None or self._locked or not (
                typing_ok or may_open(self._clock.now(), self._typed_at)):
            return None
        if typing_ok and (self._strip_visible or self._panel_open):
            self.close_panel()  # one Glance window by the field at a time: the toolbar now
        inputs = self._chip_inputs(s)
        tones = () if s.gated else self._call("icon_tones", self.icon_tones, default=())
        return icon_toolbar(self.status_icon(), check_chord,
                            "none" if s.gated else inputs.ai, tuple(tones or ()))

    def status_icon(self) -> StatusView:
        """What the status icon shows for the current field right now."""
        s = self.current
        inputs = self._chip_inputs(s)
        return self._status(s, inputs, None if s is None else self._content(s, inputs))

    # ------------------------------------------------------------------ extension

    def add_listener(self, listener: object) -> None:
        self._listeners.append(listener)

    def recheck(self) -> None:
        """Run the grammar check on the current text again: harper-ls just came up (it can
        start after the first snapshot arrived, or restart), so nothing checked it yet."""
        s = self.current
        if s is not None and not s.gated and not self._paused and (
                not self.checks_on_request or self._is_checked(s)):
            self._on_local(s, self.generation)
        else:
            self.refresh()

    def refresh(self) -> None:
        mode = self._cfg().general.check
        if mode != self._mode:  # the tray (or a hand edit) switched it
            self._mode = mode
            self._mode_changed()
        self._recompute()
        self._render()

    def request_apply(self, edit: Edit, method: str = "auto",
                      expected_sha256: str | None = None) -> str:
        """Ask capture to apply `edit`; returns the apply id, or "" when nothing was sent.

        Nothing is sent without a live ungated field, while paused, or while another apply is
        pending. `expected_sha256` defaults to the hash of the current text; pass the hash of
        the snapshot the edit was computed on when that may be an older one.
        """
        s = self.current
        if (s is None or s.gated or self._paused or self._pending is not None
                or not self._connected()):
            return ""
        self._apply_seq += 1
        apply_id = f"a{self._apply_seq}"
        sha = text_sha256(s.text) if expected_sha256 is None else expected_sha256
        self._pending = _Pending(apply_id, s.field_id, sha, edit.replacement)
        self._strip_open = False  # an apply closes the strip
        self._pending_timer = self._clock.call_later(
            APPLY_TIMEOUT_MS, lambda: self._apply_timed_out(apply_id))
        self._banner = self._copy_offer = None
        self._capture.send_apply(ApplyRequest(apply_id, s.field_id, sha, edit, method))
        self._render()
        return apply_id

    # ------------------------------------------------------------------ from capture

    def handle_snapshot(self, s: FieldSnapshot) -> None:
        prev = self.current
        self.current = s
        self._capture_lost = False
        if prev is not None and prev.field_id == s.field_id and prev.text != s.text:
            self._typed_at = self._clock.now()
        back, self._panel_back = self._panel_back, None
        # A hotkey's key grab (Super+Alt+1…4 picking a tone) blurred the field with the
        # panel open: it is open again now the field is back.
        reopen = (back is not None and back[0] == s.field_id and self._clock.now() <= back[1]
                  and not s.gated and not self._paused)
        if prev is None or prev.field_id != s.field_id:
            self._panel_open = False
            self._strip_open = False
            self._preview_open = False
            self._banner = self._copy_offer = None
            if s.field_id != self._snoozed_field:
                self._snoozed_field = None
        if s.gated:
            self.scheduler.on_snapshot(s)  # cancels every timer for the previous text
            self._leave_text()
            if prev is not None and not prev.gated and not self._paused:
                self._emit("on_focus_lost", "gated")
            self._render()
            return
        if self._paused:
            self._render()
            return
        self._emit("on_snapshot", s)
        if self.checks_on_request:
            self._quiet_snapshot(s)
        else:
            self._typing = False
            self.scheduler.on_snapshot(s)
            if self._typing:
                self._render()
        if reopen and not self._panel_open:
            self._panel_open = self._strip_open = True
            self._render()
        check_back = self._check_back
        if check_back is not None:
            self._check_back = None
            if s.field_id == check_back[0] and self._clock.now() <= check_back[1]:
                self.check_now(with_drawer=check_back[2])
        back = self._use_back
        if back is not None:
            self._use_back = None
            fix, until = back
            if ((s.field_id, text_sha256(s.text)) == (fix.field_id, fix.sha)
                    and self._clock.now() <= until):
                self._use(s, fix.edit, fix.sha)
        # Otherwise only the caret moved (or the window): the chip and the strip stay put
        # until the scheduler settles, 50 ms later. The overlay itself follows a moving window.

    def handle_focus_lost(self, reason: str) -> None:
        now = self._clock.now()
        self._blurred_at = now if reason == "blur" else None
        prev = self.current
        self._blurred_field = None if prev is None or reason != "blur" else prev.field_id
        if (prev is not None and reason == "blur" and self._checked_at is not None
                and now - self._checked_at <= USE_BLUR_S
                and self._checked is not None and self._checked[0] == prev.field_id):
            # The check hotkey's key grab blurs the app (X sends it a NotifyGrab): the
            # check is run again (from the cache) once the field is back.
            self._check_back = (prev.field_id, now + USE_GRACE_S,
                                self._open_panel_too or self._panel_open)
        self._panel_back = (prev.field_id, now + USE_GRACE_S) \
            if prev is not None and reason == "blur" and self._panel_open else None
        self.current = None
        self._strip_open = False
        self._preview_open = False
        self._stop_open_wait()
        self.scheduler.on_focus_lost()
        self._leave_text()
        self._emit("on_focus_lost", reason)
        self._render()

    def handle_capture_connected(self) -> None:
        """The link to capture is (back) up; its first snapshot may be a long way off."""
        self._capture_lost = False
        self._render()

    def handle_capture_disconnected(self) -> None:
        self._capture_lost = True
        self.current = None
        self._clear_pending()  # its result can no longer arrive
        self._revert_of = None
        self.scheduler.on_focus_lost()
        self._leave_text()
        self._emit("on_capture_lost")
        self._render()

    def handle_apply_result(self, r: ApplyResult) -> None:
        """A result is the answer to the pending apply, to a revert, or to neither.

        Neither means late (its apply timed out) or a duplicate: the controller ignores it.
        """
        pending = self._pending
        if pending is not None and r.apply_id == pending.apply_id:
            self._clear_pending()
            if r.status == "verified":
                self._last_verified = r.apply_id
                if self._revert_of is not None:
                    self._revert_of = r.apply_id  # sent after this apply: it undoes this one
            self._apply_finished(pending, r)
        elif self._revert_of is not None and r.apply_id in (self._revert_of, ""):
            self._revert_of = None
            if r.status == "reverted":
                self._last_verified = ""
            elif r.status in ("failed", "refused"):
                self._notifier.notify(*REVERT_FAILED)
        self._emit("on_apply_result", r)

    # ------------------------------------------------------------------ user actions

    def open_panel(self) -> None:
        s = self.current
        if s is None or s.gated or self._paused or self._anchor(s) is None:
            return
        self._panel_open = True
        self._strip_open = True  # the drawer comes with the strip above it
        self._preview_open = False  # (the panel shows the tone's rewrite selected)
        self._snoozed_field = None
        self._render()

    def open_preview(self) -> None:
        """Open the strip on what `extra_preview` shows (a tone's rewrite the icon's toolbar
        asked for) instead of the corrected text: Use (Alt+Enter) applies it, Esc closes it.
        The panel is not opened."""
        s = self.current
        if s is None or s.gated or self._paused or self._anchor(s) is None:
            return
        self._preview_open = True
        self._strip_open = True
        self._panel_open = False
        self._snoozed_field = None
        self._stop_open_wait()
        self._render()

    def close_panel(self, snooze: bool = False) -> None:
        """Close the drawer and the strip the user opened. `snooze` (Esc) also keeps the
        strip from coming back by itself on this field, in "auto" mode."""
        self._panel_open = False
        self._strip_open = False
        self._preview_open = False
        self._stop_open_wait()
        self._banner = self._copy_offer = None
        if snooze and self.current is not None and self._auto():
            self._snoozed_field = self.current.field_id
        self._render()

    def toggle_strip(self) -> None:
        """The status icon was clicked: open the strip (with what it has, or one line saying
        why it has nothing), or close it when it is up."""
        s = self.current
        if s is None:
            log_event("icon_click_ignored", why="no_field")
            return
        if not self._strip_visible and (
                self._anchor(s) is None
                or self.status_icon().state is StatusState.HIDDEN):
            log_event("icon_click_ignored", why="no_icon")
            return  # no icon on screen: there was nothing to click
        if self._strip_visible:
            self._strip_open = self._panel_open = self._preview_open = False
            self._stop_open_wait()
            self._banner = self._copy_offer = None
            if self._auto():
                self._snoozed_field = s.field_id
        elif self.checks_on_request:
            self.check_now()
            return
        else:
            self._strip_open = True
            self._snoozed_field = None
        self._render()

    def check_now(self, with_drawer: bool = False) -> None:
        """Check the focused field's text as it is now: the local grammar check and, when
        an AI is ready, one analysis; the status icon shows the work, then the strip opens
        with the result (the user asked: no click on the icon is needed). Pressed again,
        with the strip open, it checks again. Works in either `[general] check` mode."""
        s = self.current
        now = self._clock.now()
        if s is None and self._blurred_field is not None and self._blurred_at is not None \
                and now - self._blurred_at <= USE_BLUR_S:
            # The grab's blur came before the key: check once the field is back.
            self._check_back = (self._blurred_field, now + USE_GRACE_S, with_drawer)
            return
        if (s is None or s.gated or self._paused or self._locked
                or self._pending is not None or not self._connected()):
            log_event("check_skipped", why=(
                "no_field" if s is None else "gated" if s.gated else "paused" if self._paused
                else "locked" if self._locked else "pending" if self._pending is not None
                else "no_capture"))
            return
        self._check_back = None
        self._checked_at = now
        gen = self.scheduler.check_now(s)
        self.requested_gen = gen
        self._checked = (s.field_id, s.text)
        self._settled = True
        self._typing = False
        self._awaiting_text = False
        self._local = []
        self._checking = True
        self._snoozed_field = None
        self._banner = self._copy_offer = None
        self._preview_open = False
        self._stop_open_wait()
        self._open_when_checked = True
        self._open_panel_too = with_drawer
        self._open_by = self._clock.now() + OPEN_WAIT_MS / 1000
        self._open_timer = self._clock.call_later(OPEN_WAIT_MS, self._open_wait_over)
        log_event("check_requested", len=len(s.text))
        self._recompute()
        # The analysis is asked for first: the strip waits for it once the local check is back.
        if self.on_analyze_due is not None and s.text.strip():
            self._call("on_analyze_due", self.on_analyze_due, s, gen)
        if self.current is s and gen == self.generation:
            self._on_local(s, gen)
        self._render()

    def toggle_panel(self) -> None:
        """The strip's "⋯": open the drawer, or close it again (the strip stays)."""
        if self._panel_open:
            self._panel_open = False
            self._banner = self._copy_offer = None
            self._render()
        else:
            self.open_panel()

    def accept_issue(self, issue_id: str, choice: int = 0) -> None:
        issue = self._find(issue_id)
        if issue is not None and 0 <= choice < len(issue.replacements):
            self.request_apply(issue_edit(issue, choice))

    def fix_all_safe(self) -> None:
        s = self.current
        if s is None or self._pending is not None:
            return
        edits: list[Edit] = []
        for i in self._issues:  # sorted by position; an edit overlapping the last one waits
            if i.safe and i.replacements and (not edits or i.start >= edits[-1].end):
                edits.append(issue_edit(i))
        if not edits:
            return
        merged = merge_edits(s.text, edits)
        if overlaps(find_protected(s.text), merged.start, merged.end):
            self._banner = FIX_INDIVIDUALLY
            self._panel_open = True  # the banner, and the way to fix one at a time
            self._render()
            return
        self.request_apply(merged)

    def use_strip(self) -> None:
        """Apply the corrected text the strip shows: one edit, one undo step."""
        for hook in self.extra_use:
            if self._call("extra_use", hook, default=False):
                return
        s, shown, at = self.current, self._strip_shown, self._blurred_at
        recent = shown is not None and at is not None and self._clock.now() - at <= USE_BLUR_S
        if s is None:
            if recent:  # the grab's own blur: apply when the field is back
                self._use_back = (shown, self._clock.now() + USE_GRACE_S)
            return
        if self._pending is not None:
            return
        if self.strip() is not None:
            fix = strip_fix(s.text, s.caret, self._issues)
            if fix is not None:
                self._use(s, fix.edit, text_sha256(s.text))
        elif recent and shown is not None and \
                (s.field_id, text_sha256(s.text)) == (shown.field_id, shown.sha):
            # The blur and the field's return both came before the key: the strip is not
            # back yet (the text is checked again), but this is the text it showed.
            self._use(s, shown.edit, shown.sha)

    def _use(self, s: FieldSnapshot, edit: Edit, sha: str) -> None:
        """Pasted where the app leaves the choice to Glance: typed over a selection, some
        editors (GtkSourceView) make every word its own undo step."""
        self._panel_open = False
        self._strip_open = False
        method = "paste" if self._cfg().app(s.wm_class).writeback == "auto" else "auto"
        self.request_apply(edit, method, expected_sha256=sha)

    def revert_last(self) -> None:
        if self._connected():
            self._revert_of = self._last_verified
            self._capture.send_revert()

    def copy_instead(self) -> None:
        """Put the replacement that could not be applied on the clipboard."""
        if self._copy_offer is not None:
            self._overlay.copy_to_clipboard(self._copy_offer)
            self._copy_offer = None
            self._render()

    def add_to_dictionary(self, issue_id: str) -> None:
        issue, s = self._find(issue_id), self.current
        if issue is None or s is None:
            return
        try:
            self._filter.dictionary.add(s.text[issue.start:issue.end])
        except ValueError:  # not a single word
            return
        self.refresh()

    def ignore_rule(self, issue_id: str, scope: str) -> None:
        if scope not in ("app", "global"):
            raise ValueError(f"unknown scope: {scope}")
        issue, s = self._find(issue_id), self.current
        if issue is None or s is None:
            return
        self._filter.ignored.ignore(issue.rule, s.wm_class if scope == "app" else None)
        self.refresh()

    def handle_resume(self) -> None:
        """The machine woke from suspend: what was true before sleep is not true now.

        Every timer and in-flight result is stale (the generation moves on), the snapshot is
        dropped and the overlay hidden, a pending apply is forgotten (its late result is
        ignored), capture is told to re-register its listeners, and listeners (`on_reset`)
        cancel AI work, re-grab hotkeys, refresh backend health and drop warm processes.
        The next snapshot from capture shows the chip again.
        """
        self.current = None
        self.scheduler.on_focus_lost()  # cancels every timer; late local/AI results are stale
        self._clear_pending()
        self._revert_of = None
        self._use_back = None
        self._strip_shown = None
        self._strip_open = False
        self._blurred_at = None
        self._leave_text()
        self._capture.send_reset()
        self._emit("on_reset")
        self._render()
        log_event("resumed_from_sleep")

    def handle_lock(self, active: bool) -> None:
        """The screen locked or unlocked: locked behaves as paused and shows nothing; the
        user's own pause, if any, is still there after the unlock."""
        if active == self._locked:
            return
        self._locked = active
        if active:
            self._lock_paused = not self._paused
            if self._lock_paused:
                self.toggle_pause()
        elif self._lock_paused:
            self._lock_paused = False
            self.resume()
        self._render()

    def handle_monitors_changed(self) -> None:
        """A monitor came or went: place everything again from the last snapshot against
        the monitors as they are now; hidden when the anchor is off every one of them."""
        self._render()

    def toggle_pause(self, seconds: int | None = None) -> None:
        if self._paused:
            self.resume()
            return
        self._paused = True
        self._strip_open = False
        self._stop_open_wait()
        if seconds is not None:
            self._pause_until = self._clock.now() + seconds
            self._pause_timer = self._clock.call_later(seconds * 1000, self.resume)
        self.scheduler.on_focus_lost()  # cancels timers; a check in flight becomes stale
        self._leave_text()
        self._emit("on_pause_changed", True)
        self._render()

    def resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        self._strip_open = False
        self._pause_until = None
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        self._emit("on_pause_changed", False)
        s = self.current
        if s is not None and not s.gated:
            self._emit("on_snapshot", s)
            if self.checks_on_request:
                self._quiet_snapshot(s)
            else:
                self.scheduler.on_snapshot(s)
        self._render()

    # ------------------------------------------------------------------ check on request

    def _is_checked(self, s: FieldSnapshot) -> bool:
        return self._checked == (s.field_id, s.text)

    def _quiet_snapshot(self, s: FieldSnapshot) -> None:
        """A snapshot in "shortcut" mode: nothing is checked. A new text makes the last
        check's result stale: it is dropped and the strip closes."""
        self._settled = True
        self._typing = False
        if not self.scheduler.on_snapshot_quiet(s):
            self._render()  # the caret (or the window) moved: the icon follows it
            return
        self._emit("on_typing")  # rewrites made for the old text are dropped
        self._awaiting_text = False
        if not self._is_checked(s):
            self._local = []
            self._checking = False
            self._stop_open_wait()
            if self._strip_open or self._panel_open:
                self._strip_open = self._panel_open = False
                self._banner = self._copy_offer = None
        self._recompute()
        self._render()

    def _stop_open_wait(self) -> None:
        self._open_when_checked = False
        self._open_panel_too = False
        if self._open_timer is not None:
            self._open_timer.cancel()
            self._open_timer = None

    def _open_wait_over(self) -> None:
        self._open_timer = None
        if self._open_when_checked:
            self._render()

    def _maybe_open(self, inputs: ChipInputs) -> None:
        """check_now's strip: open at once while the AI checks (its loader, then the quick
        local fixes as a preview, replaced in place by the AI's corrected text), else once
        the local check is back."""
        s = self.current
        if not self._open_when_checked or s is None:
            return
        if not self._is_checked(s):
            self._stop_open_wait()
            log_event("check_dropped", why="text_changed")
            return
        if self._checking and not inputs.ai_checking:
            return
        panel_too = self._open_panel_too
        self._stop_open_wait()
        log_event("check_strip_open", ai_back=not inputs.ai_pending)
        self._strip_open = True
        self._panel_open = self._panel_open or panel_too

    def _mode_changed(self) -> None:
        """`[general] check` changed while running: start (or stop) checking as you type."""
        self._checked = None
        self._stop_open_wait()
        s = self.current
        self.scheduler.on_focus_lost()  # forget the last text: the new mode starts afresh
        if s is None or s.gated or self._paused:
            return
        self._local = []
        self._checking = False
        self._strip_open = self._panel_open = False
        if self.checks_on_request:
            self._quiet_snapshot(s)
        else:
            self._typing = False
            self.scheduler.on_snapshot(s)

    # ------------------------------------------------------------------ scheduler callbacks
    # The snapshot the scheduler passes may be older than `current` (same text, earlier
    # caret): everything below works from `current`.

    def _on_typing(self) -> None:
        self._typing = True
        self._settled = False
        self._checking = True
        self._awaiting_text = False
        self._local = []
        self._recompute()
        self._emit("on_typing")

    def _on_local(self, _s: FieldSnapshot, gen: int) -> None:
        s = self.current
        if s is None or gen != self.generation:
            return
        if not self._grammar.alive:
            self._checking = False  # nothing will check it
            self._render()  # tray shows degraded
            return
        # The whole text, up to LOCAL_MAX_WORDS words: beyond that, those around the caret.
        w0, w1 = word_window(s.text, s.caret, LOCAL_MAX_WORDS)
        text = s.text[w0:w1]

        def check() -> list[Issue]:
            try:
                found = self._grammar.check(text)
            except Exception:  # noqa: BLE001 - a failed check must still end "checking"
                return []
            return found if w0 == 0 else [_shifted(i, w0) for i in found]

        self._run_bg(check, lambda found: self._local_done(gen, found))

    def _local_done(self, gen: int, found: list[Issue]) -> None:
        if gen != self.generation or self.current is None:
            return  # the text changed while the check ran
        self._checking = False
        self._local = list(found)
        self.refresh()

    def _on_settled(self, _s: FieldSnapshot, gen: int) -> None:
        if gen != self.generation or self.current is None:
            return
        self._settled = True
        self._render()

    def _on_analyze(self, _s: FieldSnapshot, gen: int) -> None:
        s = self.current
        if self.on_analyze_due is not None and s is not None and gen == self.generation:
            self._call("on_analyze_due", self.on_analyze_due, s, gen)

    # ------------------------------------------------------------------ internals

    def _connected(self) -> bool:
        return bool(self._capture.connected) and not self._capture_lost

    def _find(self, issue_id: str) -> Issue | None:
        return next((i for i in self._issues if i.id == issue_id), None)

    def _leave_text(self) -> None:
        """Forget everything derived from the text: no field, a gated one, or paused."""
        self._local = []
        self._issues = []
        self._awaiting_text = False
        self._settled = False
        self._checking = False
        self._panel_open = False
        self._preview_open = False
        self._banner = self._copy_offer = None
        self._stop_open_wait()

    def _clear_pending(self) -> None:
        self._pending = None
        if self._pending_timer is not None:
            self._pending_timer.cancel()
            self._pending_timer = None

    def _apply_timed_out(self, apply_id: str) -> None:
        pending = self._pending
        if pending is None or pending.apply_id != apply_id:
            return
        self._clear_pending()  # a result that still arrives is ignored
        log_event("apply_timeout")
        self._apply_failed(pending)
        self._render()

    def _apply_failed(self, pending: _Pending) -> None:
        s = self.current
        if s is not None and s.field_id == pending.field_id:
            self._copy_offer = pending.replacement
        self._notifier.notify(*APPLY_FAILED)

    def _apply_finished(self, pending: _Pending, r: ApplyResult) -> None:
        if r.status == "verified":
            s = self.current
            if s is not None and text_sha256(s.text) == pending.sha:
                # The issues still describe the old text: hide them until the new one arrives.
                self._awaiting_text = True
                self._recompute()
        elif r.status == "reverted":
            self._notifier.notify(*REVERTED)
        elif r.status in ("failed", "refused"):
            self._apply_failed(pending)
        # stale, unverified and anything newer: silent
        self._render()

    def _call(self, hook: str, fn: Callable[..., Any], *args: Any, default: Any = None) -> Any:
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - an extension must not take the daemon down
            log_event("hook_error", hook=hook, kind=type(e).__name__)
            return default

    def _emit(self, hook: str, *args: Any) -> None:
        for listener in list(self._listeners):
            fn = getattr(listener, hook, None)
            if fn is not None:
                self._call(hook, fn, *args)

    def _recompute(self) -> None:
        s = self.current
        if s is None or s.gated or self._paused or self._awaiting_text or (
                self.checks_on_request and not self._is_checked(s)):
            self._issues = []  # ("shortcut": nothing until the user asks for this text)
            return
        found = list(self._local)
        for provider in self.extra_issues:
            found += self._call("extra_issues", provider, s, default=[])
        found = [i for i in found if 0 <= i.start <= i.end <= len(s.text)]
        ignored: frozenset[str] = frozenset()
        for rules in self.extra_ignored:
            ignored |= self._call("extra_ignored", rules, s, default=frozenset())
        kept = self._filter.apply(s.text, found, s.wm_class, extra_ignored=ignored)
        ai_corrects = any(self._call("extra_corrects", p, s, default=False)
                          for p in self.extra_corrects)
        if not ai_corrects and any(self._call("extra_local_hidden", p, s, default=False)
                                   for p in self.extra_local_hidden):
            kept = [i for i in kept if i.source != "harper"]
        self._issues = combine_sources(s.text, kept, ai_corrects)

    def _anchor(self, s: FieldSnapshot) -> tuple[int, int] | None:
        ui = self._cfg().ui
        return place(s.caret_rect, s.prev_char_rect, s.field_rect, s.window_rect,
                     (ui.chip_px, ui.chip_px), self._overlay.monitors(), ui.chip_offset_px)

    def _chip_inputs(self, s: FieldSnapshot | None) -> ChipInputs:
        if s is None:
            return ChipInputs(paused=self._paused)
        n_local = sum(1 for i in self._issues if i.source == "harper")
        inputs = ChipInputs(
            has_field=self._connected(), paused=self._paused, gated=s.gated,
            text_len=len(s.text), settled=self._settled,
            snoozed=s.field_id == self._snoozed_field,
            n_local=n_local, n_llm=len(self._issues) - n_local)
        if s.gated or self._paused:
            return inputs
        for provider in self.extra_chip_inputs:
            inputs = self._call(
                "extra_chip_inputs", lambda i=inputs, p=provider: replace(i, **p(s)),
                default=inputs)
        if inputs.mismatch and not inputs.tone_label:
            inputs = replace(inputs, mismatch=False)  # no label to show: count the fixes
        return inputs

    def _strip(self, s: FieldSnapshot, chip: ChipView) -> StripView | None:
        """The strip for `s`, None when it is hidden: nothing to say, a quiet chip state
        (hidden, paused, private), or an apply on its way."""
        if (chip.state in (ChipState.HIDDEN, ChipState.PAUSED, ChipState.PRIVATE)
                or s.gated or self._paused or self._pending is not None
                or (self.checks_on_request and not self._is_checked(s))):
            return None
        view = strip_view(strip_fix(s.text, s.caret, self._issues))
        for transform in self.extra_strip:
            view = self._call("extra_strip", transform, s, view, default=view)
        return None if view.empty else replace(view, expanded=self._panel_open)

    def _auto(self) -> bool:
        return self._cfg().ui.suggestions == "auto"

    def _working(self) -> bool:
        """The text changed and is not checked yet, or an apply is on its way."""
        return (not self._settled or self._checking or self._awaiting_text
                or self._pending is not None)

    def _content(self, s: FieldSnapshot, inputs: ChipInputs) -> StripView | None:
        """What the strip has to say for `s`, shown or not (a snooze does not change it)."""
        # A check the user asked for has no length floor: they want this text checked.
        floor = 1 if self.checks_on_request else self._cfg().general.min_chars_chip
        chip = chip_view(replace(inputs, snoozed=False), floor)
        return self._strip(s, chip)

    def _status(self, s: FieldSnapshot | None, inputs: ChipInputs,
                content: StripView | None) -> StatusView:
        if s is None:
            return status_view(StatusInputs(paused=self._paused))
        usable = content is not None and content.can_use
        return status_view(StatusInputs(
            has_field=inputs.has_field, paused=self._paused, gated=bool(s.gated),
            # (a requested check: working until its AI analysis is back too)
            checking=self._working() or inputs.ai_checking
            or (self._open_when_checked and inputs.ai_pending),
            typing=not self._settled, analyzing=inputs.analyzing,
            n_fixes=content.n_fixes if usable and content is not None else 0,
            has_note=content is not None and not usable and not content.empty,
            ai=inputs.ai))

    def _visible_strip(self, s: FieldSnapshot, content: StripView | None,
                       status: StatusView) -> StripView | None:
        """The strip as it should be on screen: by itself in "auto" mode (unless snoozed),
        else only while the user has it open - then with one line when it has nothing.
        A muted state (paused, private, no AI) never opens it by itself, in either mode:
        its line is shown only after a click on the icon."""
        if not self._strip_open:
            if (not self._auto() or s.field_id == self._snoozed_field
                    or status.state in (StatusState.MUTED, StatusState.HIDDEN)):
                return None
            return content
        if self._pending is not None:
            return None
        if content is not None:
            return replace(content, on_demand=True)
        menu = True
        if status.state is StatusState.MUTED:
            line = status.message or NOTHING_LINE
            menu = not (self._paused or s.gated)
        elif status.state is StatusState.WORKING:
            line = CHECKING_LINE
        else:
            line = NOTHING_LINE
        return StripView(note=line, expanded=self._panel_open, menu=menu, on_demand=True)

    def _preview(self, s: FieldSnapshot) -> StripView | None:
        """The open preview's strip; once it has nothing to show (the text changed, the
        rewrite went away) the preview closes, and the strip with it."""
        if not self._preview_open:
            return None
        if not (s.gated or self._paused or self._pending is not None):
            for provider in self.extra_preview:
                view = self._call("extra_preview", provider, s, default=None)
                if view is not None:
                    return replace(view, on_demand=True)
        self._strip_open = self._preview_open = False
        return None

    def _render(self) -> None:
        if self._locked:
            self._overlay.hide_chip()
            self._overlay.hide_status()
            self._overlay.hide_strip()
            self._overlay.hide_panel()
            self._strip_shown = None
            self._strip_visible = False
            self._set_tray(self.current, self._chip_inputs(self.current))
            return
        s = self.current
        cfg = self._cfg()
        inputs = self._chip_inputs(s)
        self._maybe_open(inputs)
        view = chip_view(inputs, cfg.general.min_chars_chip)
        xy = self._anchor(s) if s is not None else None
        if view.state is ChipState.HIDDEN or xy is None:
            self._overlay.hide_chip()
        else:
            self._overlay.show_chip(view, xy[0], xy[1])

        anchor = None if s is None else Anchor(s.caret_rect, s.prev_char_rect, s.field_rect,
                                               s.window_rect, s.pid, _tail(s.text, s.caret))
        content = None if s is None or xy is None else self._content(s, inputs)
        status = self._status(s, inputs, content)
        if status.state is StatusState.HIDDEN or anchor is None or xy is None:
            self._overlay.hide_status()
        else:
            self._overlay.show_status(status, anchor)

        preview = None if s is None or xy is None else self._preview(s)
        strip = preview if preview is not None or s is None or xy is None else \
            self._visible_strip(s, content, status)
        hold = (preview is None and self._strip_open and self._strip_visible
                and content is None
                and self._working())  # typing in an open strip: it updates once checked
        if hold:
            pass
        elif strip is None or anchor is None or s is None:
            self._overlay.hide_strip()
            self._strip_visible = False
            at = self._blurred_at
            if s is not None and (at is None or self._clock.now() - at > USE_BLUR_S):
                self._strip_shown = None  # nothing to use (a blur, and the return, keep it)
        else:
            fix = strip_fix(s.text, s.caret, self._issues) \
                if strip.can_use and preview is None else None
            self._strip_shown = None if fix is None else \
                _Shown(s.field_id, text_sha256(s.text), fix.edit)
            self._overlay.show_strip(strip, anchor)
            self._strip_visible = True

        if self._panel_open and s is not None and xy is not None:
            panel = build_panel(s.text, self._issues, self._banner, self._copy_offer)
            for transform in self.extra_panel:
                panel = self._call("extra_panel", transform, s, panel, default=panel)
            below = cfg.ui.chip_px + cfg.ui.chip_offset_px
            self._overlay.show_panel(panel, xy[0], xy[1] + below, anchor=anchor)
        else:
            self._overlay.hide_panel()

        self._set_tray(s, inputs)

    def _set_tray(self, s: FieldSnapshot | None, inputs: ChipInputs) -> None:
        if self._paused:
            state = "paused"
        elif not self._connected() or not self._grammar.alive:
            state = "degraded"
        elif s is not None and s.gated:
            state = "private"
        elif inputs.ai == "none":
            state = "no_ai"
        else:
            state = "active"
        if state != self._tray_state:
            self._tray_state = state
            self._tray.set_state(state)



def _shifted(i: Issue, by: int) -> Issue:
    """An issue found in a window of the text, at its place in the whole text."""
    start, end = i.start + by, i.end + by
    return replace(i, id=f"{i.source}:{i.rule}:{start}:{end}", start=start, end=end)


def _tail(text: str, caret: int) -> bool:
    """Text follows the caret on its line (so the line may run to the field's right edge)."""
    end = text.find("\n", caret)
    return bool(text[caret:len(text) if end < 0 else end].strip())
