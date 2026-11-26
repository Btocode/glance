"""ctl messages for the acceptance tests; they exist only while `GLANCE_TEST=1`.

    test_state  -> {"ok", "snapshot": FieldSnapshot dict WITH its text | None, "chip": {...},
                    "issues": [issue id, ...]}   (ids are `source:rule:start:end`, no text)
    test_apply  {start, end, replacement, method} -> {"ok", "apply_id"} | {"ok": False, ...}
    test_apply_result {apply_id} -> {"ok", "result": {apply_id, status, detail} | None}
    test_resume -> {"ok"}   the wake-from-suspend path (what logind's PrepareForSleep(false)
                            triggers), without a real suspend
    test_lock {active} -> {"ok"}   the screen-lock path

`handle_control` in `daemon/main.py` calls `handle_test_message` first; a None answer means
"not a test message" (or tests are off), so the production path never sees these requests.
"""
from __future__ import annotations

import os
from typing import Any

from glance.protocol import Edit
from glance.ui.chip_model import chip_view

METHODS = ("auto", "type", "paste")


def handle_test_message(controller: Any, msg_type: str, payload: dict[str, Any],
                        min_chars: int = 15) -> dict[str, Any] | None:
    if os.environ.get("GLANCE_TEST") != "1" or msg_type not in (
            "test_state", "test_apply", "test_apply_result", "test_resume", "test_lock"):
        return None
    if msg_type == "test_resume":
        controller.handle_resume()
        return {"ok": True}
    if msg_type == "test_lock":
        controller.handle_lock(payload.get("active") is True)
        return {"ok": True}
    _record_results(controller)
    if msg_type == "test_apply_result":
        got = controller._test_results.get(payload.get("apply_id"))
        return {"ok": True, "result": None if got is None else got.to_dict()}
    if msg_type == "test_state":
        s = controller.current
        view = chip_view(controller._chip_inputs(s), min_chars)
        return {"ok": True, "snapshot": None if s is None else s.to_dict(),
                "chip": {"state": view.state.value, "label": view.label, "badge": view.badge},
                "issues": [i.id for i in controller.issues],
                "strip": s is not None and getattr(controller, "strip", lambda: None)() is not None}
    try:
        start, end = payload["start"], payload["end"]
        replacement, method = payload["replacement"], payload["method"]
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or not isinstance(replacement, str) or method not in METHODS):
            raise ValueError
    except (KeyError, ValueError):
        return {"ok": False, "error": "bad request"}
    apply_id = controller.request_apply(Edit(start, end, replacement), method)
    return {"ok": True, "apply_id": apply_id} if apply_id else {"ok": False, "error": "not sent"}


def _record_results(controller: Any) -> None:
    """Remember every ApplyResult the controller sees, so a test can read the outcome."""
    if hasattr(controller, "_test_results"):
        return
    controller._test_results = {}
    original = controller.handle_apply_result

    def handle(r: Any) -> None:
        controller._test_results[r.apply_id] = r
        original(r)

    controller.handle_apply_result = handle
