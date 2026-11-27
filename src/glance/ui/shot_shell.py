"""Glance Shot on GNOME Wayland: the picker and the bar are drawn by Glance's shell extension
(extension/shot.js), the capture is `Shell.Screenshot.screenshot_area` inside the shell.

No portal dialog and no file: the extension answers `ShotCapture` with the PNG (base64) and
only to the verified glanced (extension.js refuses every other caller, glance-capture
included). The picker reports through the daemon method `ShotPicked`; clicks on the bar come
back as `Clicked` with target "shot" (Ask's question and Mark's strokes ride along as `args`).

Ask is typed in the card itself (`inline_ask`: the extension's entry takes the keyboard with a
modal grab inside the shell while it is open, the app stays the focused window). Mark shows
the frozen region in the shell (the PNG handed over as a 0600 file in $XDG_RUNTIME_DIR, gone
once the extension has read it); the strokes come back as point lists and the marked PNG is
drawn here, by the same code as on X11 (`shot_gtk.marked_png`).
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import math
import os
import tempfile
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
ASK_DX, ASK_DY = 8, 48  # a one-line entry of its own ("Other…"): just under the toolbar
MARK_KEYS = frozenset({"pen", "box", "arrow", "highlight"})
MAX_STROKES, MAX_POINTS = 500, 20_000  # per mark-up, per stroke: far beyond a hand's


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


def _strokes(raw: Any) -> list[tuple[str, list[tuple[float, float]]]]:
    """Mark's strokes from the extension, checked: [[tool, [[x, y], ...]], ...] in the
    region's logical coordinates."""
    out: list[tuple[str, list[tuple[float, float]]]] = []
    for item in raw[:MAX_STROKES] if isinstance(raw, list) else []:
        if not (isinstance(item, list) and len(item) == 2 and item[0] in MARK_KEYS
                and isinstance(item[1], list)):
            continue
        pts = []
        for p in item[1][:MAX_POINTS]:
            if (isinstance(p, list) and len(p) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in p)):
                pts.append((float(p[0]), float(p[1])))
        if pts:
            out.append((item[0], pts))
    return out


class ShellShot:
    """ShotUiPort over the extension. `on_action(key)` (or `on_action(key, args)` when the
    click carries some: Ask's question) gets the bar's clicks."""

    inline_ask = True  # Ask's question is typed in the card (the extension's entry)

    def __init__(self, bus: ShellBus, tokens: Callable[[], dict[str, str]],
                 on_action: Callable[..., None]) -> None:
        self._bus = bus
        self._tokens = tokens
        self._act = on_action
        self._on_done: Callable[[PickResult | None], None] | None = None
        self._card: tuple[int, ...] | None = None  # the card on screen: x, y, w, h
        # Mark while it is up: the shot and the controller's `done`
        self._marking: tuple[Shot, Callable[[bytes | None], None]] | None = None

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
                   "words": {"title": bm.TITLE, "esc": look.ESC_KEY, "close": look.CLOSE,
                             "hint": bm.HINT, "hint_sub": bm.HINT_SUB,
                             "return": look.RETURN_KEY, "ask_prompt": view.ask_prompt,
                             "ask_placeholder": view.ask_placeholder, "ask_go": view.ask_go,
                             "ask_back": "back", "glyphs": bm.GLYPHS}}
        try:
            answer = self._bus.call("ShotBar", payload)
        except ShellUnavailable:
            return
        r = answer.get("rect") if isinstance(answer, dict) else None
        try:
            self._card = tuple(int(v) for v in r) if isinstance(r, list) and len(r) == 4 \
                else self._card
        except (TypeError, ValueError):
            pass

    def ask_point(self) -> tuple[int, int] | None:
        """Where a one-line entry of its own goes ("Other…" language): under the toolbar
        (the extension reports where it put the card)."""
        if self._card is None:
            return None
        x, y = self._card[0], self._card[1]
        return x + ASK_DX, y + ASK_DY

    def hide_bar(self) -> None:
        self._card = None
        self._marking = None  # the extension closes Mark with the bar
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
        extra = args.get("args")
        if action == "mark_done":
            self._mark_end(_strokes(extra.get("strokes")) if isinstance(extra, dict) else [])
        elif action == "mark_cancel":
            self._mark_end([])
        elif isinstance(action, str):
            if isinstance(extra, dict):
                self._act(action, extra)
            else:
                self._act(action)
        return True

    # ------------------------------------------------------------------ Mark

    def annotate(self, shot: Shot, done: Callable[[bytes | None], None]) -> None:
        """Mark up the shot in the shell; `done` gets the marked PNG, or None (cancelled,
        nothing drawn, or the extension could not show it)."""
        from glance.ui.shot_gtk import (
            HIGHLIGHT_WIDTH,
            MARK_HIGHLIGHT,
            MARK_INK,
            MARK_TOOLS,
            MARK_WIDTH,
        )

        self._marking = None
        run = os.environ.get("XDG_RUNTIME_DIR")
        if not run or not os.path.isdir(run):
            log_event("shot_mark_unavailable", why="runtime_dir")
            done(None)
            return
        r = shot.rect
        answer = None
        try:
            fd, path = tempfile.mkstemp(prefix="glance-mark-", suffix=".png", dir=run)  # 0600
        except OSError:
            done(None)
            return
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(shot.png)
            answer = self._bus.call("ShotBar", {"mark": {
                "path": path, "rect": [r.x, r.y, r.w, r.h], "tokens": self._tokens(),
                "look": SHOT_LOOK,
                "tools": [[k, label, glyph] for k, label, glyph in MARK_TOOLS],
                "words": {"undo": "Undo", "cancel": "Cancel", "done": "Done"},
                "ink": list(MARK_INK), "highlight": list(MARK_HIGHLIGHT),
                "width": MARK_WIDTH, "highlight_width": HIGHLIGHT_WIDTH}})
        except (ShellUnavailable, OSError):
            answer = None
        finally:
            try:
                os.unlink(path)  # the extension read it during the call
            except OSError:
                pass
        if isinstance(answer, dict) and answer.get("ok"):
            self._marking = (shot, done)
        else:
            log_event("shot_mark_unavailable", why="shell")
            done(None)

    def _mark_end(self, strokes: list[tuple[str, list[tuple[float, float]]]]) -> None:
        m, self._marking = self._marking, None
        if m is None:
            return
        shot, done = m
        png = None
        if strokes:
            from glance.ui.shot_gtk import marked_png

            try:
                png = marked_png(shot, strokes)
            except Exception as e:  # noqa: BLE001 - cairo's errors, a bad PNG
                log_event("shot_mark_failed", kind=type(e).__name__)
        done(png)

    # ------------------------------------------------------------------ clipboard

    def copy_image(self, png: bytes) -> bool:
        try:
            answer = self._bus.call("ShotClipboard",
                                    {"png": base64.b64encode(png).decode("ascii")})
        except ShellUnavailable:
            return False
        return bool(isinstance(answer, dict) and answer.get("ok"))
