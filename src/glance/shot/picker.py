"""The region picker as a small state machine the renderers drive (X11 and macOS; the GNOME
extension has the same rules in JavaScript, extension/shot.js).

The screen dims; a drag selects a rectangle and a size label follows the pointer. Enter takes
the monitor under the pointer, Space the window under it, Esc cancels. A press and release
without a drag selects nothing (the picker stays up).
"""
from __future__ import annotations

from dataclasses import dataclass

from glance.protocol import Rect
from glance.shot import geometry as geo

HINT = "Drag to capture · Enter: full screen · Space: window · Esc: cancel"


@dataclass(frozen=True)
class Outcome:
    """What the picker ended with: a region (screen coordinates) or a cancel."""

    region: Rect | None  # None: cancelled
    how: str = "drag"    # drag | screen | window | cancel


class Picker:
    def __init__(self, monitors: list[Rect], windows: list[Rect] | None = None) -> None:
        """`windows`: the top-level windows' frames, topmost first (for Space)."""
        self.monitors = list(monitors) or [Rect(0, 0, 1, 1)]
        self.screen = geo.bounds(self.monitors)
        self.windows = list(windows or [])
        self.pointer = (self.screen.x + self.screen.w // 2, self.screen.y + self.screen.h // 2)
        self._start: tuple[int, int] | None = None
        self.selection: Rect | None = None  # the rectangle being dragged
        self.done: Outcome | None = None

    # ---------------------------------------------------------------- input

    def press(self, x: int, y: int) -> None:
        if self.done is None:
            self.pointer = (x, y)
            self._start = (x, y)
            self.selection = None

    def motion(self, x: int, y: int) -> None:
        if self.done is not None:
            return
        self.pointer = (x, y)
        if self._start is not None:
            r = geo.drag_rect(*self._start, x, y)
            self.selection = geo.intersect(r, self.screen) if geo.is_region(r) else None

    def release(self, x: int, y: int) -> Outcome | None:
        if self.done is not None or self._start is None:
            return self.done
        self.motion(x, y)
        self._start = None
        sel, self.selection = self.selection, None
        if sel is not None:
            self.done = Outcome(sel, "drag")
        return self.done

    def key(self, name: str) -> Outcome | None:
        """`Return`/`Enter`, `space`, `Escape` (X keysym names, any case)."""
        if self.done is not None:
            return self.done
        k = name.casefold()
        if k in ("escape", "esc"):
            self.done = Outcome(None, "cancel")
        elif k in ("return", "enter", "kp_enter"):
            mon = geo.full_screen(*self.pointer, self.monitors)
            if mon is not None:
                self.done = Outcome(mon, "screen")
        elif k == "space":
            win = geo.window_under(*self.pointer, self.windows, self.screen)
            if win is not None and geo.is_region(win):
                self.done = Outcome(win, "window")
        return self.done

    def cancel(self) -> Outcome:
        if self.done is None:
            self.done = Outcome(None, "cancel")
        return self.done

    # ---------------------------------------------------------------- what to draw

    @property
    def dragging(self) -> bool:
        return self._start is not None

    def hover_window(self) -> Rect | None:
        """The window Space would take, outlined while nothing is being dragged."""
        if self.dragging:
            return None
        return geo.window_under(*self.pointer, self.windows, self.screen)

    def label(self) -> str | None:
        return geo.size_label(self.selection) if self.selection is not None else None

    def label_at(self, size: tuple[int, int]) -> tuple[int, int]:
        return geo.label_position(*self.pointer, size, self.screen)
