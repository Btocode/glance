"""`test_overlay`: what the status icon, the strip and the drawer show and where, for the end-to-end tests.
It exists only while `GLANCE_TEST=1` (None otherwise: not a test message)."""
from __future__ import annotations

import os
from typing import Any


def handle_test_overlay(msg_type: str, payload: dict[str, Any], overlay: Any,
                        controller: Any, env: Any = None) -> dict[str, Any] | None:
    env = os.environ if env is None else env
    if msg_type != "test_overlay" or env.get("GLANCE_TEST") != "1":
        return None
    strip = controller.strip()
    geometry = overlay.strip_geometry()
    panel = overlay.panel_geometry()
    status = overlay.status_geometry()
    return {"ok": True,
            "strip": None if geometry is None else list(geometry),
            "strip_text": overlay.strip_text(),
            "can_use": bool(strip is not None and strip.can_use),
            "panel": None if panel is None else list(panel),
            "enter_grabbed": bool(getattr(overlay, "use_grabbed", False)),
            "escape_grabbed": bool(getattr(overlay, "escape_grabbed", False)),
            "status": None if status is None else list(status),
            "status_state": overlay.status_state(),
            "controller_status": controller.status_icon().state.value}
