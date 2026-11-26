"""Glance Shot on GNOME Wayland: the picker and the bar are drawn by Glance's shell extension
(extension/shot.js), the capture is `Shell.Screenshot.screenshot_area` inside the shell.

No portal dialog and no file: the extension answers `ShotCapture` with the PNG (base64) and
only to the verified glanced (extension.js refuses every other caller, glance-capture
included). The picker reports through the daemon method `ShotPicked`; clicks on the bar come
back as `Clicked` with target "shot".
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
from collections.abc import Callable
from typing import Any

from glance.log import log_event
from glance.protocol import Rect
from glance.shellbus import ShellBus, ShellUnavailable
from glance.shot import bar_model as bm
from glance.shot.controller import PickResult
from glance.shot.image import Shot, png_size
from glance.shot.picker import HINT
from glance.ui import look
from glance.ui.ports import WindowInfo
from glance.ui.scene import LOOK
from glance.ui.theme import FONT, MONO

CAPTURE_TIMEOUT_MS = 10_000
SHOT_LOOK = {**LOOK, "font": FONT, "mono": MONO}
MAX_PNG_B64 = 96 * 1024 * 1024  # an 8K screen as PNG is far below this


def _windows(raw: Any) -> tuple[WindowInfo, ...]:
    out = []
    for w in raw if isinstance(raw, list) else []:
        if not isinstance(w, dict):
            continue
        r = w.get("rect")
        try:
            rect = Rect(*(int(v) for v in r)) if isinstance(r, list) and len(r) == 4 else None
        except (TypeError, ValueError):
            rect = None
        out.append(WindowInfo(str(w.get("wm_class") or ""), str(w.get("title") or ""),
                              int(w.get("pid") or 0) if str(w.get("pid") or "0").isdigit() else 0,
                              0, rect))
    return tuple(out)


class ShellShot:
    """ShotUiPort over the extension. `on_action(key)` gets the bar's clicks."""

    def __init__(self, bus: ShellBus, tokens: Callable[[], dict[str, str]],
                 on_action: Callable[[str], None]) -> None:
        self._bus = bus
        self._tokens = tokens
        self._act = on_action
        self._on_done: Callable[[PickResult | None], None] | None = None

    # ------------------------------------------------------------------ the picker

    def pick(self, on_done: Callable[[PickResult | None], None]) -> None:
        self._on_done = on_done
        try:
            answer = self._bus.call("ShotPick", {"tokens": self._tokens(), "hint": HINT,
                                                 "look": SHOT_LOOK})
        except ShellUnavailable:
            answer = None
        if not (isinstance(answer, dict) and answer.get("ok")):
            log_event("shot_shell_unavailable")
            self._finish(None)

    def cancel_pick(self) -> None:
        if self._on_done is not None:
            self._on_done = None
            try:
                self._bus.call("ShotCancel")
            except ShellUnavailable:
                pass

    def on_picked(self, payload: dict[str, Any]) -> None:
        """`ShotPicked` from the extension: the region (or null), the windows on screen."""
        if self._on_done is None:
            return
        raw = payload.get("rect")
        try:
            region = Rect(*(int(v) for v in raw)) if isinstance(raw, list) and len(raw) == 4 \
                else None
        except (TypeError, ValueError):
            region = None
        if region is None or region.w < 1 or region.h < 1:
            self._finish(None)
            return
        png = self.capture(region)
        if png is None:
            self._finish(None)
            return
        try:
            w, h = png_size(png)
        except ValueError:
            self._finish(None)
            return
        self._finish(PickResult(Shot(png, w, h, region), _windows(payload.get("windows"))))

    def capture(self, region: Rect) -> bytes | None:
        try:
            answer = self._bus.call("ShotCapture", {"x": region.x, "y": region.y,
                                                    "w": region.w, "h": region.h},
                                    timeout_ms=CAPTURE_TIMEOUT_MS)
        except ShellUnavailable:
            return None
        data = answer.get("png") if isinstance(answer, dict) and answer.get("ok") else None
        if not isinstance(data, str) or len(data) > MAX_PNG_B64:
            log_event("shot_capture_failed")
            return None
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None

    def _finish(self, result: PickResult | None) -> None:
        done, self._on_done = self._on_done, None
        if done is not None:
            done(result)

    # ------------------------------------------------------------------ the bar

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None:
        payload = {"view": dataclasses.asdict(view),
                   "region": [region.x, region.y, region.w, region.h],
                   "tokens": self._tokens(), "look": SHOT_LOOK,
                   "words": {"title": bm.TITLE, "esc": look.ESC_KEY, "close": look.CLOSE}}
        try:
            self._bus.call("ShotBar", payload)
        except ShellUnavailable:
            pass

    def hide_bar(self) -> None:
        try:
            self._bus.call("ShotBar", {"hide": True})
        except ShellUnavailable:
            pass

    def on_clicked(self, payload: dict[str, Any]) -> bool:
        """A `Clicked` from the extension: True when it was the bar's."""
        if payload.get("target") != "shot":
            return False
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        action = args.get("action")
        if isinstance(action, str):
            self._act(action)
        return True

    # ------------------------------------------------------------------ clipboard

    def copy_image(self, png: bytes) -> bool:
        try:
            answer = self._bus.call("ShotClipboard",
                                    {"png": base64.b64encode(png).decode("ascii")})
        except ShellUnavailable:
            return False
        return bool(isinstance(answer, dict) and answer.get("ok"))
