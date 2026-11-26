"""FocusTracker: follows the focused field and turns it into SNAPSHOT / FOCUS_LOST messages.

The privacy gate is evaluated on metadata before the field's text is read; a gated field is
never read. Only an editable field is a field at all: a focused page body, document, link or
label (anything without the `editable` state; a password field counts by `protected`) is
reported as no field, never as a gated one, so nothing is shown for it.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from glance.capture.ports import A11yField, A11yPort, A11yTimeout
from glance.clock import Clock, Handle
from glance.privacy.gate import FieldMeta, PrivacyGate
from glance.protocol import FOCUS_LOST, SNAPSHOT, FieldSnapshot, Rect

CONFIDENCE_WITH_CARET = 1.0
CONFIDENCE_WITHOUT_CARET = 0.7


def is_field(meta: FieldMeta) -> bool:
    """An editable text field (a password field is one too: it is `protected`)."""
    return bool({"editable", "protected"} & meta.states)


class FocusTracker:
    def __init__(
        self,
        port: A11yPort,
        gate: PrivacyGate,
        emit: Callable[[str, dict], None],
        clock: Clock,
        coalesce_ms: int = 16,
    ) -> None:
        self._port = port
        self._gate = gate
        self._emit = emit
        self._clock = clock
        self._coalesce_ms = coalesce_ms
        self._field: A11yField | None = None
        self._timer: Handle | None = None
        self._seq = 0
        self._live = False  # the last message was a SNAPSHOT (a FOCUS_LOST is owed on leaving)

    def start(self) -> None:
        self._port.start(self._on_focus, self._on_change)
        field = self._port.focused()
        if field is not None:
            self._on_focus(field)

    def current_field(self) -> A11yField | None:
        return self._field

    def set_gate(self, gate: PrivacyGate) -> None:
        """Swap the gate (config hot reload) and re-evaluate the current field."""
        self._gate = gate
        self.resync()

    def resync(self) -> None:
        """Emit a fresh snapshot of the current field (e.g. after the daemon reconnects)."""
        if self._field is not None:
            self._schedule()

    def resync_all(self) -> None:
        """After a reset: find the focused field again and report it (or its absence)."""
        self._cancel()
        self._field = None
        field = self._port.focused()
        if field is not None:
            self._on_focus(field)

    def _on_focus(self, field: A11yField | None) -> None:
        had_field = self._field is not None
        self._field = field
        if field is not None:
            self._schedule()
        else:
            self._cancel()
            if had_field:
                self._live = False
                self._emit(FOCUS_LOST, {"reason": "blur"})

    def _on_change(self, field: A11yField) -> None:
        # After a timeout `_field` is None, so the field stays unread until it is focused again.
        if self._field is not None and field.key == self._field.key:
            self._schedule()

    def _schedule(self) -> None:
        if self._timer is None:
            self._timer = self._clock.call_later(self._coalesce_ms, self._fire)

    def _cancel(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        self._timer = None
        field = self._field
        if field is None:
            return
        try:
            snapshot = self._capture(field)
        except A11yTimeout:
            self._field = None
            self._live = False
            self._emit(FOCUS_LOST, {"reason": "timeout"})
            return
        if snapshot is None:
            # Not an editable field (a page body being read, a link): no field. Followed
            # still, in case it becomes editable.
            if self._live:
                self._live = False
                self._emit(FOCUS_LOST, {"reason": "blur"})
            return
        self._live = True
        self._emit(SNAPSHOT, snapshot.to_dict())

    def _gate_reason(self, meta: FieldMeta) -> str | None:
        decision = self._gate.evaluate(meta)
        if not decision.allowed:
            return decision.reason
        if meta.url is not None and not meta.title.strip():
            # Web content whose frame name cannot be read: the private-window marker lives
            # only there (findings S2.7), so its absence must not be taken for "not private".
            return "title_unknown"
        return None

    def _capture(self, field: A11yField) -> FieldSnapshot | None:
        meta = field.meta()
        if not is_field(meta):
            return None
        reason = self._gate_reason(meta)
        if reason is not None:
            # Where the field is (never what it holds), so its icon sits by the field.
            return self._snapshot(field, meta, gated=reason, field_rect=field.field_extents(),
                                  window_rect=field.window_rect())
        read = field.read()
        # The length known before the read can be short of the truth (a rich editor's root
        # reports its blocks, not their text), so the gate sees the real length as well.
        reason = self._gate_reason(replace(meta, text_len=len(read.text)))
        if reason is not None:
            return self._snapshot(field, meta, gated=reason, field_rect=read.field_rect,
                                  window_rect=read.window_rect)
        return self._snapshot(
            field, meta, text=read.text, caret=read.caret, caret_rect=read.caret_rect,
            prev_char_rect=read.prev_char_rect, field_rect=read.field_rect,
            window_rect=read.window_rect,
        )

    def _snapshot(
        self,
        field: A11yField,
        meta: FieldMeta,
        *,
        gated: str | None = None,
        text: str = "",
        caret: int = 0,
        caret_rect: Rect | None = None,
        prev_char_rect: Rect | None = None,
        field_rect: Rect | None = None,
        window_rect: Rect | None = None,
    ) -> FieldSnapshot:
        self._seq += 1
        return FieldSnapshot(
            app=meta.app, wm_class=meta.wm_class,
            # A gated field's window title and URL are content too (a private tab's page).
            title="" if gated else meta.title, url=None if gated else meta.url,
            role=meta.role, text=text, caret=caret, caret_rect=caret_rect,
            prev_char_rect=prev_char_rect, field_rect=field_rect, window_rect=window_rect,
            field_id=field.key,
            capture_confidence=(
                CONFIDENCE_WITH_CARET if caret_rect is not None else CONFIDENCE_WITHOUT_CARET
            ),
            seq=self._seq, pid=meta.pid, gated=gated,
        )
