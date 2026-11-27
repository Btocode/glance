"""Selection actions: the floating mark beside selected text and its action bar. Pure; the
three renderers (GTK on X11, the GNOME Shell extension, macOS) draw exactly these views.

A mouse selection that has settled (`SETTLE_MS` after the button is up) shows the small
Glance mark just above-right of where the pointer let go. Nothing has been read: the mark is
placed from the pointer alone. Hovering it for `OPEN_MS`, or a click, turns it into the
action bar; only a click on an action reads the selection. The mark (and a bar nobody is
pointing at) goes away after `IDLE_MS`, and at once on a key, a scroll, a click elsewhere, a
focus change or the selection going away.
"""
from __future__ import annotations

from dataclasses import dataclass

from glance.protocol import Rect

SETTLE_MS = 350        # a selection is "settled" this long after the last change
IDLE_MS = 6000         # the mark (or an unattended bar) hides after this
OPEN_MS = 250          # resting on the mark this long opens the bar
TYPING_QUIET_MS = 800  # no mark when the field's text changed this recently
MARK_PX = 18           # the brand mark ...
MARK_BOX = (28, 28)    # ... in its click target
MARK_GAP = (6, 8)      # right of and above the point where the button went up
BAR_H = 40             # the bar's height in one row (each renderer sizes it exactly)
EXCERPT_CHARS = 90     # the selection as shown in the result panel's header


@dataclass(frozen=True)
class BarAction:
    """One button: `key` goes back as the `sel_action` panel action's `key`."""

    key: str
    label: str
    glyph: str | None = None  # a `look.GLYPHS` name
    tip: str | None = None


EXPLAIN = BarAction("explain", "Explain", "spark")
SUMMARIZE = BarAction("summarize", "Summarize", "lines")
FIX = BarAction("fix", "Fix grammar", "check")
REWRITE = BarAction("rewrite", "Rewrite…", "pen")
REPLY = BarAction("reply", "Reply…", "reply")
COPY = BarAction("copy", "Copy", "copy")
MORE = BarAction("more", "⋯", None, "More: translate to another language, tones")
TRANSLATE_TO = BarAction("translate_to", "Translate to…", "globe")

# Actions that read the selection and need an AI (Fix grammar tries the local checker first).
AI_ACTIONS = frozenset({"explain", "summarize", "translate", "reply", "translate_to"})
# Actions whose result is the selection transformed: "Replace selection" may be offered.
REPLACING = frozenset({"translate", "translate_to", "fix", "tone"})


def translate(target: str) -> BarAction:
    return BarAction("translate", "Translate", "globe", f"Translate to {target}")


@dataclass(frozen=True)
class SelectionBarView:
    """`state`: `mark` (the bare mark) or `bar` (the actions). `more` holds the second row
    ("Translate to…" and the tone chips) while `⋯` is open."""

    state: str
    actions: tuple[BarAction, ...] = ()
    more: tuple[BarAction, ...] = ()
    translate_to: str = "English"
    hint: str | None = None  # "Super+Alt+A" muted in the bar's corner


def mark_view() -> SelectionBarView:
    return SelectionBarView("mark")


def bar_view(translate_to: str, tones: tuple[tuple[str, str], ...] = (),
             more_open: bool = False, hint: str | None = None) -> SelectionBarView:
    """The action bar. `tones` are (key, label) of the audience's tone chips, offered under
    `⋯` as one-click rewrites."""
    actions = (EXPLAIN, SUMMARIZE, translate(translate_to), FIX, REWRITE, REPLY, COPY, MORE)
    more: tuple[BarAction, ...] = ()
    if more_open:
        from glance.ui.look import tone_glyph

        more = (TRANSLATE_TO, *(BarAction(f"tone:{k}", label, tone_glyph(k)) for k, label in tones))
    return SelectionBarView("bar", actions, more, translate_to, hint)


def place_mark(x: int, y: int, mons: list[Rect]) -> tuple[int, int]:
    """The mark's top-left for a selection that ended at (x, y): above-right of it, or below
    when there is no room above; always on that monitor."""
    w, h = MARK_BOX
    gx, gy = MARK_GAP
    mon = _monitor(x, y, mons)
    mx, my = x + gx, y - gy - h
    if mon is None:
        return mx, max(0, my)
    if my < mon.y:
        my = y + gy + 8  # under the line instead
    mx = max(mon.x, min(mx, mon.x + mon.w - w))
    my = max(mon.y, min(my, mon.y + mon.h - h))
    return mx, my


def place_bar(mark_xy: tuple[int, int], size: tuple[int, int],
              mons: list[Rect]) -> tuple[int, int]:
    """The bar's top-left: it grows rightwards from the mark, over it; pushed left and up
    to stay on the mark's monitor."""
    x, y = mark_xy
    w, h = size
    mon = _monitor(x, y, mons)
    if mon is None:
        return x, y
    x = max(mon.x, min(x, mon.x + mon.w - w))
    y = max(mon.y, min(y, mon.y + mon.h - h))
    return x, y


RESULT_BELOW = 16      # the result panel's top: this far under the point (clear of its line)
RESULT_ABOVE = 22      # ... or its bottom this far over it, with no room below
RESULT_LEFT = 32       # its left edge: this far left of the point
RESULT_RESERVE_H = 360  # the side (below / above) is picked for this much height


def place_result(point: tuple[int, int], size: tuple[int, int], mons: list[Rect],
                 reserve_h: int = RESULT_RESERVE_H) -> tuple[int, int]:
    """Top-left of the result panel (Explain, Translate…) for a selection that ended at
    `point`: under that line, never over it. With no room under it for the panel's full
    height (`reserve_h`) it goes above the line instead. The side is picked for the full
    height, so the panel does not jump as its answer comes in: it grows down (or, above the
    line, up). Kept whole on the point's monitor."""
    x, y = point
    w, h = size
    mon = _monitor(x, y, mons)
    px, py = x - RESULT_LEFT, y + RESULT_BELOW
    if mon is None:
        return px, py
    if py + max(h, reserve_h) > mon.y + mon.h and y - RESULT_ABOVE - max(h, reserve_h) >= mon.y:
        py = y - RESULT_ABOVE - h
    px = max(mon.x, min(px, mon.x + mon.w - w))
    py = max(mon.y, min(py, mon.y + mon.h - h))
    return px, py


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[:limit - 1].rstrip() + "…"


def words(text: str) -> int:
    return len(text.split())


def _monitor(x: int, y: int, mons: list[Rect]) -> Rect | None:
    for m in mons:
        if m.x <= x < m.x + m.w and m.y <= y < m.y + m.h:
            return m
    return mons[0] if mons else None
