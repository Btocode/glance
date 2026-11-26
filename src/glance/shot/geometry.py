"""The region picker's geometry, the same for every renderer: the dragged rectangle, the size
label that follows the pointer, the window under the pointer, and where the bar goes."""
from __future__ import annotations

from collections.abc import Iterable

from glance.protocol import Rect

MIN_SIDE = 4  # a drag smaller than this either way is a click, not a region
LABEL_OFFSET = (16, 18)  # the size label, right of and below the pointer
BAR_GAP = 8  # between the region and the bar


def drag_rect(x0: int, y0: int, x1: int, y1: int) -> Rect:
    """The rectangle a drag from (x0, y0) to (x1, y1) spans, whichever way it went."""
    return Rect(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))


def is_region(r: Rect) -> bool:
    return r.w >= MIN_SIDE and r.h >= MIN_SIDE


def intersect(a: Rect, b: Rect) -> Rect | None:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return None
    return Rect(x0, y0, x1 - x0, y1 - y0)


def overlaps(a: Rect, b: Rect) -> bool:
    return intersect(a, b) is not None


def bounds(monitors: Iterable[Rect]) -> Rect:
    """The rectangle around every monitor (the whole desktop)."""
    mons = list(monitors)
    if not mons:
        return Rect(0, 0, 0, 0)
    x0, y0 = min(m.x for m in mons), min(m.y for m in mons)
    x1, y1 = max(m.x + m.w for m in mons), max(m.y + m.h for m in mons)
    return Rect(x0, y0, x1 - x0, y1 - y0)


def monitor_at(x: int, y: int, monitors: list[Rect]) -> Rect | None:
    for m in monitors:
        if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
            return m
    return monitors[0] if monitors else None


def full_screen(px: int, py: int, monitors: list[Rect]) -> Rect | None:
    """Enter: the monitor under the pointer."""
    return monitor_at(px, py, monitors)


def window_under(px: int, py: int, windows: Iterable[Rect],
                 screen: Rect | None = None) -> Rect | None:
    """Space: the topmost window (`windows` top first) under the pointer, cut to the screen."""
    for w in windows:
        if w.x <= px < w.x + w.w and w.y <= py < w.y + w.h:
            return intersect(w, screen) if screen is not None else w
    return None


def size_label(r: Rect) -> str:
    """"640 × 480" (logical pixels)."""
    return f"{r.w} × {r.h}"


def label_position(px: int, py: int, size: tuple[int, int], screen: Rect) -> tuple[int, int]:
    """Top-left of the size label: below-right of the pointer, flipped to the other side
    near the screen's right or bottom edge."""
    w, h = size
    dx, dy = LABEL_OFFSET
    x, y = px + dx, py + dy
    if x + w > screen.x + screen.w:
        x = px - dx - w
    if y + h > screen.y + screen.h:
        y = py - dy - h
    return max(screen.x, x), max(screen.y, y)


def bar_position(region: Rect, size: tuple[int, int], monitors: list[Rect]) -> tuple[int, int]:
    """Top-left of the shot bar: under the region, right-aligned with it; above it when there
    is no room below; inside its bottom edge when there is room neither side. Kept whole on
    the region's monitor."""
    w, h = size
    mon = monitor_at(region.x + region.w // 2, region.y + region.h // 2, monitors) \
        or Rect(region.x, region.y, max(region.w, w), region.h + h + BAR_GAP)
    x = region.x + region.w - w
    below = region.y + region.h + BAR_GAP
    above = region.y - BAR_GAP - h
    if below + h <= mon.y + mon.h:
        y = below
    elif above >= mon.y:
        y = above
    else:
        y = region.y + region.h - h - BAR_GAP
    x = max(mon.x, min(x, mon.x + mon.w - w))
    y = max(mon.y, min(y, mon.y + mon.h - h))
    return x, y
