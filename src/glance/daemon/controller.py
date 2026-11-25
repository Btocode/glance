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
from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue, issue_edit, merge_edits
from glance.log import log_event
from glance.privacy.entities import find_protected, overlaps
from glance.protocol import ApplyRequest, ApplyResult, Edit, FieldSnapshot, text_sha256
from glance.ui.chip_model import ChipInputs, ChipState, ChipView, chip_view
from glance.ui.panel_model import PanelView, build_panel
from glance.ui.placement import Anchor, place
from glance.ui.ports import CapturePort, GrammarPort, NotifierPort, OverlayPort, TrayPort
from glance.ui.strip_model import StripView, strip_fix, strip_view

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
      extra_status       providers of extra `status()` entries (numbers, bools, short enums)
      add_listener(obj)  obj may define on_snapshot(s), on_typing(), on_focus_lost(reason),
                         on_apply_result(r), on_pause_changed(paused), on_capture_lost(),
                         on_reset()
      refresh()          recompute the issues and redraw; call after a provider's data changed
      request_apply()    send an edit through the verified write-back path
      use_strip()        apply what the strip shows (its "Use" button, Alt+Enter)
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
        self.extra_status: list[Callable[[], dict[str, Any]]] = []
        self._listeners: list[object] = []

        self._local: list[Issue] = []  # the grammar port's issues for the current text
        self._issues: list[Issue] = []
        self._awaiting_text = False  # an apply was verified; its new text has not arrived yet
        self._typing = False
        self._settled = False
        self._snoozed_field: str | None = None
        self._panel_open = False
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
        self._lock_paused = False  # ...and the lock, not the user, paused us

    # ------------------------------------------------------------------ read-only state

    @property
    def generation(self) -> int:
        return self.scheduler.generation

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
        return None if s is None else self._strip(s, self.chip())

    # ------------------------------------------------------------------ extension

    def add_listener(self, listener: object) -> None:
        self._listeners.append(listener)

    def recheck(self) -> None:
        """Run the grammar check on the current text again: harper-ls just came up (it can
        start after the first snapshot arrived, or restart), so nothing checked it yet."""
        s = self.current
        if s is not None and not s.gated and not self._paused:
            self._on_local(s, self.generation)
        else:
            self.refresh()

    def refresh(self) -> None:
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
        if prev is None or prev.field_id != s.field_id:
            self._panel_open = False
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
        self._typing = False
        self.scheduler.on_snapshot(s)
        if self._typing:
            self._render()
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
        self._blurred_at = self._clock.now() if reason == "blur" else None
        self.current = None
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
        self._snoozed_field = None
        self._render()

    def close_panel(self, snooze: bool = False) -> None:
        self._panel_open = False
        self._banner = self._copy_offer = None
        if snooze and self.current is not None:
            self._snoozed_field = self.current.field_id
        self._render()

    def toggle_panel(self) -> None:
        """The strip's "⋯": open the drawer, or close it again."""
        if self._panel_open:
            self.close_panel()
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
        """Apply the corrected sentence the strip shows: one edit, one undo step."""
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
        self._pause_until = None
        if self._pause_timer is not None:
            self._pause_timer.cancel()
            self._pause_timer = None
        self._emit("on_pause_changed", False)
        s = self.current
        if s is not None and not s.gated:
            self._emit("on_snapshot", s)
            self.scheduler.on_snapshot(s)
        self._render()

    # ------------------------------------------------------------------ scheduler callbacks
    # The snapshot the scheduler passes may be older than `current` (same text, earlier
    # caret): everything below works from `current`.

    def _on_typing(self) -> None:
        self._typing = True
        self._settled = False
        self._awaiting_text = False
        self._local = []
        self._recompute()
        self._emit("on_typing")

    def _on_local(self, _s: FieldSnapshot, gen: int) -> None:
        s = self.current
        if s is None or gen != self.generation:
            return
        if not self._grammar.alive:
            self._render()  # tray shows degraded
            return
        text = s.text
        self._run_bg(lambda: self._grammar.check(text), lambda found: self._local_done(gen, found))

    def _local_done(self, gen: int, found: list[Issue]) -> None:
        if gen != self.generation or self.current is None:
            return  # the text changed while the check ran
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
        self._panel_open = False
        self._banner = self._copy_offer = None

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
        if s is None or s.gated or self._paused or self._awaiting_text:
            self._issues = []
            return
        found = list(self._local)
        for provider in self.extra_issues:
            found += self._call("extra_issues", provider, s, default=[])
        found = [i for i in found if 0 <= i.start <= i.end <= len(s.text)]
        ignored: frozenset[str] = frozenset()
        for rules in self.extra_ignored:
            ignored |= self._call("extra_ignored", rules, s, default=frozenset())
        kept = self._filter.apply(s.text, found, s.wm_class, extra_ignored=ignored)
        local = [i for i in kept if i.source == "harper"]
        # The local checker wins where another source flags the same text.
        others = [i for i in kept if i.source != "harper"
                  and not any(i.start < h.end and h.start < i.end for h in local)]
        self._issues = sorted(local + others, key=lambda i: (i.start, i.end))

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
                or s.gated or self._paused or self._pending is not None):
            return None
        view = strip_view(strip_fix(s.text, s.caret, self._issues))
        for transform in self.extra_strip:
            view = self._call("extra_strip", transform, s, view, default=view)
        return None if view.empty else replace(view, expanded=self._panel_open)

    def _render(self) -> None:
        if self._locked:
            self._overlay.hide_chip()
            self._overlay.hide_strip()
            self._overlay.hide_panel()
            self._strip_shown = None
            self._set_tray(self.current, self._chip_inputs(self.current))
            return
        s = self.current
        cfg = self._cfg()
        inputs = self._chip_inputs(s)
        view = chip_view(inputs, cfg.general.min_chars_chip)
        xy = self._anchor(s) if s is not None else None
        if view.state is ChipState.HIDDEN or xy is None:
            self._overlay.hide_chip()
        else:
            self._overlay.show_chip(view, xy[0], xy[1])

        anchor = None if s is None else Anchor(s.caret_rect, s.prev_char_rect, s.field_rect,
                                               s.window_rect, s.pid)
        strip = None if s is None or xy is None else self._strip(s, view)
        if strip is None or anchor is None or s is None:
            self._overlay.hide_strip()
            at = self._blurred_at
            if s is not None and (at is None or self._clock.now() - at > USE_BLUR_S):
                self._strip_shown = None  # nothing to use (a blur, and the return, keep it)
        else:
            fix = strip_fix(s.text, s.caret, self._issues) if strip.can_use else None
            self._strip_shown = None if fix is None else \
                _Shown(s.field_id, text_sha256(s.text), fix.edit)
            self._overlay.show_strip(strip, anchor)

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

