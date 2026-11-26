"""MacOverlay: the OverlayPort in AppKit — status icon, collapsed strip, drawer — and MacGhost.

Everything is placed by the shared `place_status` / `place_strip` in top-left coordinates
and converted to AppKit's bottom-left space only when a panel is moved (`appkit_util.place`).
The panels never activate Glance and never take the keyboard, except the "Rewrite as…" ask
panel. Colours are `ui/theme.py`'s tokens, light or dark with the system appearance.

Needs PyObjC (AppKit); imported only on a Mac, in glanced.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import objc
from AppKit import (
    NSAttributedString,
    NSBezelStyleRegularSquare,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSMutableAttributedString,
    NSPasteboard,
    NSPasteboardTypeString,
    NSScrollView,
    NSTextField,
    NSView,
    NSWorkspace,
)
from Foundation import NSMakePoint, NSMakeRect

from glance.grammar.harper import SPELL_RULE
from glance.log import log_event
from glance.platform.macos.appkit_util import (
    Target,
    color,
    frame_of,
    make_panel,
    monitors,
    place,
    tokens,
)
from glance.platform.macos.hotkeys_mac import symbols
from glance.protocol import Rect
from glance.ui.chip_model import ChipView
from glance.ui.panel_model import PanelView, ToneChip
from glance.ui.placement import Anchor, clamp_to_monitor, place_status, place_strip
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import StripView, scroll_max_h

ICON = 22
ICON_X, ICON_Y = 1, 5
ICON_BOX = (30, 28)
BADGE_C, BADGE_R = (23.0, 7.0), 6.5
SPIN_MS = 125
DRAWER_MIN_W = 520
GAP = 4
PAD = 10
FONT_PX = 13.0
ASK_PLACEHOLDER = "e.g. shorter, friendlier, in Spanish"
_LINE_FIXES = 3
_ASK_WAIT_MS = 1500


def _font(bold: bool = False, size: float = FONT_PX) -> Any:
    return NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)


def _attr(text: str, fg: Any, bold: bool = False, size: float = FONT_PX) -> Any:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _font(bold, size), NSForegroundColorAttributeName: fg})


def cgcolor(hex_colour: str) -> Any:
    from Quartz import CGColorCreateSRGB

    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return CGColorCreateSRGB(r, g, b, 1.0)


class _Flipped(NSView):
    """A view with a top-left origin, so layout reads like the GTK overlay's."""

    def isFlipped(self):
        return True


class _Card(_Flipped):
    """A rounded card: the strip (mint) or the drawer (surface)."""

    def drawRect_(self, _rect):
        fill = getattr(self, "fill", None)
        border = getattr(self, "border", None)
        if fill is None:
            return
        b = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, b.size.width - 1, b.size.height - 1), 10, 10)
        fill.setFill()
        path.fill()
        if border is not None:
            border.setStroke()
            path.setLineWidth_(1)
            path.stroke()

    def acceptsFirstMouse_(self, _event):
        return True


class _Icon(_Flipped):
    """The status icon. `state` is set by the overlay; a click calls `on_click`."""

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, _event):
        cb = getattr(self, "on_click", None)
        if cb is not None:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log_event("ui_action_error", kind=type(e).__name__)

    def drawRect_(self, _rect):
        view = getattr(self, "view", None)
        t = getattr(self, "tokens", None)
        if view is None or t is None:
            return
        circle = NSMakeRect(ICON_X + 0.5, ICON_Y + 0.5, ICON - 1, ICON - 1)
        ring = NSBezierPath.bezierPathWithOvalInRect_(circle)
        state = view.state
        if state is StatusState.READY:
            color(t["accent"]).setFill()
            ring.fill()
            self._mark(color(t["on_accent"]))
        elif state is StatusState.MUTED:
            color(t["surface"]).setFill()
            ring.fill()
            color(t["dot_quiet"]).setStroke()
            ring.setLineWidth_(1.5)
            ring.stroke()
            slash = NSBezierPath.bezierPath()
            slash.moveToPoint_(NSMakePoint(ICON_X + 6, ICON_Y + 6))
            slash.lineToPoint_(NSMakePoint(ICON_X + ICON - 6, ICON_Y + ICON - 6))
            slash.setLineWidth_(1.5)
            slash.stroke()
        else:
            color(t["strip"]).setFill()
            ring.fill()
            color(t["strip_border"]).setStroke()
            ring.setLineWidth_(1.5)
            ring.stroke()
            self._mark(color(t["accent"]))
            if state is StatusState.WORKING:
                phase = int(getattr(self, "phase", 0))
                cx, cy = ICON_X + ICON / 2, ICON_Y + ICON / 2
                arc = NSBezierPath.bezierPath()
                start = phase * 30.0
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    NSMakePoint(cx, cy), ICON / 2 - 1.5, start, start + 100, False)
                color(t["accent"]).setStroke()
                arc.setLineWidth_(2.0)
                arc.stroke()
        badge = view.badge
        if badge is not None:
            d = BADGE_R * 2
            rect = NSMakeRect(BADGE_C[0] - BADGE_R, BADGE_C[1] - BADGE_R, d, d)
            dot = NSBezierPath.bezierPathWithOvalInRect_(rect)
            color(t["surface"]).setFill()
            dot.fill()
            color(t["accent"]).setStroke()
            dot.setLineWidth_(1.2)
            dot.stroke()
            s = _attr(badge, color(t["accent"]), bold=True, size=8.5)
            size = s.size()
            s.drawAtPoint_(NSMakePoint(BADGE_C[0] - size.width / 2,
                                       BADGE_C[1] - size.height / 2))

    @objc.python_method
    def _mark(self, fg: Any) -> None:
        """Glance's mark: a small four-point sparkle in the middle of the circle."""
        cx, cy, r = ICON_X + ICON / 2, ICON_Y + ICON / 2, 5.5
        p = NSBezierPath.bezierPath()
        p.moveToPoint_(NSMakePoint(cx, cy - r))
        p.curveToPoint_controlPoint1_controlPoint2_(NSMakePoint(cx + r, cy),
                                                    NSMakePoint(cx + 1, cy - 1),
                                                    NSMakePoint(cx + 1, cy - 1))
        p.curveToPoint_controlPoint1_controlPoint2_(NSMakePoint(cx, cy + r),
                                                    NSMakePoint(cx + 1, cy + 1),
                                                    NSMakePoint(cx + 1, cy + 1))
        p.curveToPoint_controlPoint1_controlPoint2_(NSMakePoint(cx - r, cy),
                                                    NSMakePoint(cx - 1, cy + 1),
                                                    NSMakePoint(cx - 1, cy + 1))
        p.curveToPoint_controlPoint1_controlPoint2_(NSMakePoint(cx, cy - r),
                                                    NSMakePoint(cx - 1, cy - 1),
                                                    NSMakePoint(cx - 1, cy - 1))
        fg.setFill()
        p.fill()


class _AskField(NSTextField):
    pass


class _Button(NSButton):
    """A button that takes the click that reaches a panel of an inactive app (Glance never
    activates for the strip or the drawer)."""

    def acceptsFirstMouse_(self, _event):
        return True


def _label(text: str, fg: Any, width: float, bold: bool = False, lines: int = 0,
           size: float = FONT_PX) -> Any:
    lab = NSTextField.labelWithString_(text)
    lab.setFont_(_font(bold, size))
    lab.setTextColor_(fg)
    lab.setSelectable_(False)
    lab.setMaximumNumberOfLines_(lines)
    lab.cell().setWraps_(True)
    lab.cell().setLineBreakMode_(NSLineBreakByWordWrapping if lines != 1
                                 else NSLineBreakByTruncatingTail)
    lab.setPreferredMaxLayoutWidth_(width)
    _fit(lab, width)
    return lab


def _fit(view: Any, width: float) -> None:
    size = view.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 10000)).height
    view.setFrame_(NSMakeRect(0, 0, width, max(16.0, size)))


class _Rows:
    """Lays views out top to bottom; each row is one full-width view or a run of small ones
    that wraps at the width."""

    def __init__(self, parent: Any, width: float, top: float = PAD, left: float = PAD) -> None:
        self.parent, self.width, self.y, self.left = parent, width, top, left

    def full(self, view: Any, gap: float = 6) -> None:
        f = view.frame()
        view.setFrameOrigin_(NSMakePoint(self.left, self.y))
        self.parent.addSubview_(view)
        self.y += f.size.height + gap

    def run(self, views: list[Any], gap: float = 6, right: list[Any] | None = None) -> None:
        x, row_h = self.left, 0.0
        limit = self.left + self.width
        right = right or []
        rw = sum(v.frame().size.width + gap for v in right)
        for v in views:
            w, h = v.frame().size.width, v.frame().size.height
            if x > self.left and x + w > limit - (rw if v is views[-1] else 0):
                self.y += row_h + gap
                x, row_h = self.left, 0.0
            v.setFrameOrigin_(NSMakePoint(x, self.y))
            self.parent.addSubview_(v)
            x += w + gap
            row_h = max(row_h, h)
        rx = limit
        for v in reversed(right):
            w, h = v.frame().size.width, v.frame().size.height
            rx -= w
            v.setFrameOrigin_(NSMakePoint(rx, self.y))
            self.parent.addSubview_(v)
            rx -= gap
            row_h = max(row_h, h)
        self.y += row_h + gap


class MacOverlay:
    """OverlayPort for macOS. `on_panel_action(action, args)` gets the same actions as the
    GTK overlay's (use, more, accept, fix_all, add_dict, ignore, tone_apply, tone, profile,
    copy, close, guard_silence; sel_copy, sel_paste in a select-anything panel)."""

    def __init__(self, on_chip_click: Callable[[], None],
                 on_panel_action: Callable[[str, dict[str, Any]], None],
                 clock: Any, use_key: Callable[[], str] = lambda: "Alt+Return") -> None:
        self._on_chip_click = on_chip_click
        self._act = on_panel_action
        self._clock = clock
        self._use_key = use_key
        self._target = Target.alloc().init()
        self._status_view: StatusView | None = None
        self._strip_view: StripView | None = None
        self._panel_view: PanelView | None = None
        self._anchor: Anchor | None = None
        self._panel_anchor: Anchor | None = None
        self._panel_point = (0, 0)
        self._details_open = False
        self._profiles_open = False
        self._spin: Any = None
        self._phase = 0
        self._ask: dict[str, Any] | None = None
        self._strip_built_w = 0.0
        self._panel_built_w = 0.0
        self._t = tokens()

        self._icon = make_panel()
        self._icon.setHasShadow_(False)
        self._icon_view = _Icon.alloc().initWithFrame_(NSMakeRect(0, 0, *ICON_BOX))
        self._icon_view.on_click = lambda: self._on_chip_click()
        self._icon.setContentView_(self._icon_view)
        self._strip = make_panel()
        self._panel = make_panel()

    # ------------------------------------------------------------------ OverlayPort

    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        """Nothing is drawn for the chip any more (the status icon is)."""

    def hide_chip(self) -> None:
        pass

    def show_status(self, view: StatusView, anchor: Anchor) -> None:
        if view.state is StatusState.HIDDEN:
            self.hide_status()
            return
        self._anchor = anchor
        self._restyle()
        if view != self._status_view:
            self._status_view = view
            self._icon_view.view = view
            self._icon_view.tokens = self._t
            self._icon_view.setNeedsDisplay_(True)
        self._place_icon()
        self._animate()

    def hide_status(self) -> None:
        self._icon.orderOut_(None)
        self._status_view = None
        self._animate()

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        rebuilt = view != self._strip_view
        self._strip_view = view
        self._anchor = anchor
        self._layout(force=rebuilt)

    def hide_strip(self) -> None:
        had = self._strip_view is not None
        self._strip_view = None
        self._strip.orderOut_(None)
        if had and self._panel_anchor is not None:
            self._layout(force=True)

    def show_panel(self, view: PanelView, x: int, y: int, anchor: Anchor | None = None) -> None:
        self._panel_view = view
        self._panel_anchor = anchor
        self._panel_point = (x, y)
        if anchor is not None:
            self._anchor = anchor
        self._layout(force=True)

    def hide_panel(self) -> None:
        self._panel.orderOut_(None)
        self._panel_view = None
        self._panel_anchor = None
        self._details_open = self._profiles_open = False
        if self._strip_view is not None:
            self._layout(force=True)

    def monitors(self) -> list[Rect]:
        return monitors()

    def copy_to_clipboard(self, text: str) -> None:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)

    def ask_text(self, prompt: str, on_done: Callable[[str, bool], None]) -> None:
        """A small key panel with one text field; Return submits, Esc cancels. The app that
        was frontmost is activated again before `on_done` is called."""
        self._close_ask(submit=False, reactivate=False)
        from AppKit import NSApp

        before = NSWorkspace.sharedWorkspace().frontmostApplication()
        t = self._t
        panel = make_panel(can_key=True)
        w = 420.0
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, w, 74))
        card.fill, card.border = color(t["surface"]), color(t["hairline"])
        title = _label(prompt, color(t["text"]), w - 2 * PAD, bold=True, lines=1)
        title.setFrameOrigin_(NSMakePoint(PAD, PAD))
        card.addSubview_(title)
        field = _AskField.alloc().initWithFrame_(NSMakeRect(PAD, 36, w - 2 * PAD, 26))
        field.setPlaceholderString_(ASK_PLACEHOLDER)
        field.setTarget_(self._target)
        field.setAction_("fire:")
        field.setTag_(self._target.add(lambda: self._close_ask(submit=True)))
        card.addSubview_(field)
        panel.setContentView_(card)
        panel.on_cancel = lambda: self._close_ask(submit=False)
        at = self._ask_position()
        mons = self.monitors()
        x, y = at if at is not None else (mons[0].x + 200, mons[0].y + 200) if mons else (200, 200)
        place(panel, Rect(int(x), int(y), int(w), 74))
        self._ask = {"panel": panel, "field": field, "before": before, "on_done": on_done}
        NSApp().activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)
        panel.makeFirstResponder_(field)

    # ------------------------------------------------------------------ test introspection

    def status_geometry(self) -> tuple[int, int, int, int] | None:
        return frame_of(self._icon) if self._icon.isVisible() else None

    def status_state(self) -> dict[str, Any] | None:
        v = self._status_view
        if v is None or not self._icon.isVisible():
            return None
        return {"state": v.state.value, "badge": v.badge, "spin": v.spin}

    def strip_geometry(self) -> tuple[int, int, int, int] | None:
        return frame_of(self._strip) if self._strip.isVisible() else None

    def strip_text(self) -> str | None:
        v = self._strip_view
        return None if v is None or not self._strip.isVisible() else v.text

    def panel_geometry(self) -> tuple[int, int, int, int] | None:
        return frame_of(self._panel) if self._panel.isVisible() else None

    def window_numbers(self) -> dict[str, int]:
        return {"status": int(self._icon.windowNumber()), "strip": int(self._strip.windowNumber()),
                "panel": int(self._panel.windowNumber())}

    def _layout_now(self) -> None:  # the GTK overlay's name, used by test_monitors
        self._layout(force=True)

    _layout_alias = _layout_now

    # ------------------------------------------------------------------ placing

    def _restyle(self) -> None:
        t = tokens()
        if t is not self._t:
            self._t = t
            self._icon_view.tokens = t
            self._icon_view.setNeedsDisplay_(True)
            self._strip_built_w = self._panel_built_w = 0.0

    def _place_icon(self) -> None:
        if self._status_view is None or self._anchor is None:
            return
        mons = self.monitors()
        at = place_status(self._anchor, ICON, mons)
        if at is None:
            self._icon.orderOut_(None)
            return
        x, y = clamp_to_monitor(at[0] - ICON_X, at[1] - ICON_Y, ICON_BOX, mons)
        place(self._icon, Rect(x, y, *ICON_BOX))
        if not self._icon.isVisible():
            self._icon.orderFrontRegardless()

    def _animate(self) -> None:
        v = self._status_view
        want = v is not None and v.state is StatusState.WORKING and v.spin
        if want and self._spin is None:
            self._spin = self._clock.every(SPIN_MS, self._tick)
        elif not want and self._spin is not None:
            self._spin.cancel()
            self._spin = None

    def _tick(self) -> None:
        self._phase = (self._phase + 1) % 12
        self._icon_view.phase = self._phase
        self._icon_view.setNeedsDisplay_(True)

    def _field_drawer(self) -> bool:
        return self._panel_view is not None and self._panel_anchor is not None

    def _layout(self, force: bool = False) -> None:
        self._restyle()
        mons = self.monitors()
        if self._panel_view is not None and self._panel_anchor is None:
            self._place_selection(mons)
        strip, drawer = self._strip_view is not None, self._field_drawer()
        anchor = self._anchor
        if not (strip or drawer) or anchor is None:
            return
        first = place_strip(anchor, 1, mons)
        if first is None:
            self._strip.orderOut_(None)
            if drawer:
                self._panel.orderOut_(None)
            return
        mon = next((m for m in mons if m.x <= first.x < m.x + m.w), mons[0] if mons else None)
        strip_h = self._build_strip(first.w) if strip else 0
        card_w = min(max(first.w, DRAWER_MIN_W), mon.w if mon else first.w) if drawer else 0
        card_h = self._build_drawer(card_w) if drawer else 0
        total = strip_h + (GAP if strip and drawer else 0) + card_h
        p = place_strip(anchor, total, mons)
        if p is None:
            return
        if p.above:
            card_y, strip_y = p.y, p.y + card_h + (GAP if strip else 0)
        else:
            strip_y, card_y = p.y, p.y + strip_h + (GAP if strip else 0)
        if strip:
            place(self._strip, Rect(p.x, strip_y, p.w, strip_h))
            if not self._strip.isVisible():
                self._strip.orderFrontRegardless()
        if drawer:
            x = p.x
            if mon is not None:
                x = max(mon.x, min(x, mon.x + mon.w - card_w))
            place(self._panel, Rect(x, card_y, card_w, card_h))
            if not self._panel.isVisible():
                self._panel.orderFrontRegardless()

    def _place_selection(self, mons: list[Rect]) -> None:
        w = DRAWER_MIN_W
        h = self._build_drawer(w)
        x, y = clamp_to_monitor(self._panel_point[0], self._panel_point[1] + 12, (w, h), mons)
        place(self._panel, Rect(x, y, w, h))
        if not self._panel.isVisible():
            self._panel.orderFrontRegardless()

    # ------------------------------------------------------------------ the strip

    def _button(self, title: str, fn: Callable[[], None], primary: bool = False) -> Any:
        b = _Button.buttonWithTitle_target_action_(title, self._target, "fire:")
        b.setTag_(self._target.add(fn))
        b.setRefusesFirstResponder_(True)
        if primary:  # an accent pill: a bezel colour is not honoured on every macOS
            b.setBordered_(False)
            b.setAttributedTitle_(_attr(title, color(self._t["on_accent"]), bold=True))
            b.sizeToFit()
            f = b.frame()
            b.setFrameSize_((f.size.width + 22, max(24.0, f.size.height + 6)))
            b.setWantsLayer_(True)
            b.layer().setBackgroundColor_(cgcolor(self._t["accent"]))
            b.layer().setCornerRadius_(6.0)
            return b
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setFont_(_font())
        b.sizeToFit()
        return b

    def _act_button(self, title: str, action: str, primary: bool = False, **args: Any) -> Any:
        return self._button(title, lambda: self._act(action, dict(args)), primary)

    def _link(self, title: str, fn: Callable[[], None], strong: bool = False) -> Any:
        b = _Button.buttonWithTitle_target_action_(title, self._target, "fire:")
        b.setTag_(self._target.add(fn))
        b.setBordered_(False)
        b.setAttributedTitle_(_attr(title, color(self._t["accent"]), bold=strong))
        b.sizeToFit()
        b.setRefusesFirstResponder_(True)
        return b

    def _build_strip(self, width: float) -> int:
        view = self._strip_view
        assert view is not None
        t = self._t
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 40))
        warn = view.guard is not None and not view.segments
        card.fill = color(t["warn_bg"] if warn else t["strip"])
        card.border = color(t["warn_border"] if warn else t["strip_border"])
        self._strip_buttons_reset()
        buttons = []
        if view.can_use:
            buttons.append(self._act_button(f"Use {symbols(self._use_key())}", "use",
                                            primary=True))
        if view.menu:
            buttons.append(self._act_button("⋯", "more"))
        bw = sum(b.frame().size.width + 6 for b in buttons)
        text_w = max(80.0, width - 2 * PAD - bw)
        rows = _Rows(card, text_w)
        if view.segments:
            s = NSMutableAttributedString.alloc().init()
            for seg in view.shown:
                s.appendAttributedString_(_attr(
                    seg.text, color(t["accent"] if seg.changed else t["body"]), seg.changed))
            scroll = view.expanded and view.compact  # "⋯": the whole corrected text
            lab = _label("", color(t["body"]), text_w, lines=0 if scroll else 5)
            lab.setAttributedStringValue_(s)
            _fit(lab, text_w)
            lab.setAccessibilityIdentifier_("glance-strip-sentence")
            if scroll:
                rows.full(self._scrolled(lab, text_w), gap=2)
            else:
                rows.full(lab, gap=2)
        for line in (view.summary if view.segments else None, view.meta):
            if line is not None:
                rows.full(_label(line, color(t["muted"]), text_w, size=11.5), gap=2)
        if view.guard is not None:
            rows.full(_label(view.guard, color(t["warn_text"]), text_w), gap=2)
        if view.note is not None and not view.segments:
            rows.full(_label(view.note, color(t["muted"]), text_w), gap=2)
        h = max(rows.y + PAD - 2, 40.0)
        x = width - PAD
        for b in reversed(buttons):
            bw_ = b.frame().size.width
            x -= bw_
            b.setFrameOrigin_(NSMakePoint(x, (h - b.frame().size.height) / 2))
            card.addSubview_(b)
            x -= 6
        card.setFrameSize_((width, h))
        self._strip.setContentView_(card)
        return int(h)

    def _scrolled(self, view: Any, width: float) -> Any:
        """`view` in a vertical scroller no taller than SCROLL_SHARE of the monitor."""
        h = view.frame().size.height
        f = frame_of(self._strip) if self._strip.isVisible() else None
        max_h = scroll_max_h(self.monitors(), *(f[:2] if f else (None, None)))
        doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, width, h))  # top first
        view.setFrameOrigin_(NSMakePoint(0, 0))
        doc.addSubview_(view)
        scroller = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, min(h, max_h)))
        scroller.setHasVerticalScroller_(True)
        scroller.setDrawsBackground_(False)
        scroller.setDocumentView_(doc)
        return scroller

    def _strip_buttons_reset(self) -> None:
        # Tags index into the target's list; the strip and the drawer are rebuilt together
        # often enough that the list is simply never trimmed below what is on screen.
        if len(self._target.callbacks) > 2000:
            self._target.reset()

    # ------------------------------------------------------------------ the drawer

    def _card_button(self, chip: ToneChip, action: str, width: float) -> Any:
        t = self._t
        if chip.state == "loading" and not chip.preview:
            line = "Rewriting…"
        elif chip.state == "failed":
            line = "Did not work"
        elif chip.preview:
            line = " ".join(chip.preview.split())
        else:
            line = "Click to rewrite"
        title = chip.label + (f"  {chip.hotkey}" if chip.hotkey is not None else "")
        s = NSMutableAttributedString.alloc().init()
        s.appendAttributedString_(_attr(title + "\n", color(t["text"]), bold=True))
        s.appendAttributedString_(_attr(line[:40], color(t["muted"]), size=11.5))
        b = _Button.buttonWithTitle_target_action_("", self._target, "fire:")
        b.setTag_(self._target.add(lambda: self._act(action, {"key": chip.key})))
        b.setBezelStyle_(NSBezelStyleRegularSquare)
        b.setAttributedTitle_(s)
        b.setAlignment_(0)
        b.setFrame_(NSMakeRect(0, 0, width, 48))
        b.setRefusesFirstResponder_(True)
        if chip.preview:
            b.setToolTip_(chip.preview)
        b.setAccessibilityIdentifier_(f"card:{chip.key}")
        return b

    def _build_drawer(self, width: float) -> int:
        view = self._panel_view
        assert view is not None
        t = self._t
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
        card.fill, card.border = color(t["surface"]), color(t["hairline"])
        inner = width - 2 * PAD
        rows = _Rows(card, inner)
        if view.mode == "selection":
            self._fill_selection(rows, view, inner)
        else:
            self._fill_drawer(rows, view, inner)
        h = rows.y + PAD
        card.setFrameSize_((width, h))
        self._panel.setContentView_(card)
        return int(h)

    def _header(self, rows: _Rows, view: PanelView, title: str | None) -> None:
        t = self._t
        left = []
        if title:
            left.append(_label(title, color(t["text"]), 200, bold=True, lines=1))
        if view.tone:
            tone = f"Reads {view.tone.lower()}" + (f" — {view.tone_reason}"
                                                     if view.tone_reason else "")
            left.append(_label(tone, color(t["muted"]), rows.width - 160, lines=2))
        right = []
        if view.profile_label:
            if view.mode == "selection":
                right.append(_label(f"For {view.profile_label}", color(t["muted"]), 140,
                                    lines=1))
            else:
                right.append(self._link(f"For {view.profile_label} ▾", self._toggle_profiles))
        right.append(self._act_link("Close", "close"))
        for lab in left:
            lab.sizeToFit()
        rows.run(left, right=right)

    def _act_link(self, title: str, action: str, strong: bool = False, **args: Any) -> Any:
        return self._link(title, lambda: self._act(action, dict(args)), strong)

    def _cards(self, rows: _Rows, chips: tuple[ToneChip, ...], action: str, inner: float) -> None:
        n = max(1, len(chips))
        w = (inner - 8 * (n - 1)) / n
        rows.run([self._card_button(c, action, w) for c in chips], gap=8)

    def _fill_drawer(self, rows: _Rows, view: PanelView, inner: float) -> None:
        t = self._t
        if view.guard:
            rows.run([_label(view.guard, color(t["warn_text"]), inner - 260, lines=2)],
                     right=[self._act_link("Don't warn about this here", "guard_silence",
                                           scope="app"),
                            self._act_link("Never", "guard_silence", scope="global")])
        self._header(rows, view, None)
        if view.profile_label and view.profiles and self._profiles_open:
            rows.run([self._act_button(label, "profile", name=name)
                      for name, label in view.profiles])
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        if cards:
            self._cards(rows, cards, "tone_apply", inner)
        if any(c.key == "custom" for c in view.tone_chips):
            rows.run([_label("Rewrite as…", color(t["muted"]), 90, lines=1),
                      self._act_button(ASK_PLACEHOLDER, "tone", key="custom")])
        if view.banner:
            rows.full(_label(view.banner, color(t["warn_text"]), inner))
        if view.copy_offer is not None:
            rewrite = view.preview_state is not None
            rows.run([_label("The rewrite was not typed into the field." if rewrite
                             else "The change could not be applied here.",
                             color(t["muted"]), inner - 140, lines=2)],
                     right=[self._act_button("Copy rewrite" if rewrite else "Copy instead",
                                             "copy")])
        if view.issues:
            n = len(view.issues)
            line = [self._link(f"{n} fix{'' if n == 1 else 'es'}", self._toggle_details)]
            for row in [r for r in view.issues if r.replacements][:_LINE_FIXES]:
                flagged = (row.flagged or row.excerpt).replace("\n", " ")
                line.append(self._act_link(f"{flagged} → {row.replacements[0] or '(remove)'}",
                                           "accept", issue_id=row.issue_id, choice=0))
            right = [self._act_link("Fix all", "fix_all", strong=True)] if view.can_fix_all \
                else []
            rows.run(line, right=right)
            if self._details_open:
                for row in view.issues:
                    rows.full(_label(row.excerpt.replace("\n", " "), color(t["text"]), inner),
                              gap=2)
                    if row.message:
                        rows.full(_label(row.message, color(t["muted"]), inner), gap=2)
                    fixes = [self._act_button(r or "Remove", "accept", issue_id=row.issue_id,
                                              choice=i)
                             for i, r in enumerate(row.replacements[:6])]
                    fixes += [self._act_link("Ignore here", "ignore", issue_id=row.issue_id,
                                             scope="app"),
                              self._act_link("Ignore everywhere", "ignore",
                                             issue_id=row.issue_id, scope="global")]
                    if row.rule == SPELL_RULE:
                        fixes.append(self._act_link("Add to dictionary", "add_dict",
                                                    issue_id=row.issue_id))
                    rows.run(fixes)
        elif not (view.banner or view.copy_offer is not None or view.tone_chips or view.guard):
            rows.full(_label("Nothing to fix here.", color(t["muted"]), inner))

    def _fill_selection(self, rows: _Rows, view: PanelView, inner: float) -> None:
        t = self._t
        self._header(rows, view, "Selected text")
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        if cards:
            self._cards(rows, cards, "tone", inner)
        if any(c.key == "custom" for c in view.tone_chips):
            rows.run([_label("Rewrite as…", color(t["muted"]), 90, lines=1),
                      self._act_button(ASK_PLACEHOLDER, "tone", key="custom")])
        if view.preview is not None:
            s = NSMutableAttributedString.alloc().init()
            for op in view.preview:
                if op.kind == "del":
                    continue
                ins = op.kind == "ins"
                s.appendAttributedString_(_attr(op.text, color(t["accent"] if ins
                                                               else t["body"]), ins))
            lab = _label("", color(t["body"]), inner, lines=8)
            lab.setAttributedStringValue_(s)
            _fit(lab, inner)
            rows.full(lab)
        if view.preview_state == "loading":
            rows.full(_label("Rewriting…", color(t["muted"]), inner))
        if view.banner:
            rows.full(_label(view.banner, color(t["warn_text"]), inner))
        for row in view.issues[:8]:
            rows.full(_label(row.excerpt.replace("\n", " "), color(t["text"]), inner), gap=2)
            if row.message:
                rows.full(_label(row.message, color(t["muted"]), inner), gap=4)
        right = []
        if view.can_paste:
            right.append(self._act_button("Paste over selection", "sel_paste"))
        right.append(self._act_button("Copy rewrite", "sel_copy", primary=True))
        rows.run([], right=right)

    def _toggle_details(self) -> None:
        self._details_open = not self._details_open
        self._layout(force=True)

    def _toggle_profiles(self) -> None:
        self._profiles_open = not self._profiles_open
        self._layout(force=True)

    # ------------------------------------------------------------------ ask

    def _ask_position(self) -> tuple[int, int] | None:
        if self._panel.isVisible():
            x, y, _w, h = frame_of(self._panel)
            return x, y + h + 6
        if self._strip.isVisible():
            x, y, _w, h = frame_of(self._strip)
            return x, y + h + 6
        return None

    def _close_ask(self, submit: bool, reactivate: bool = True) -> None:
        ask, self._ask = self._ask, None
        if ask is None:
            return
        text = str(ask["field"].stringValue() or "")
        ask["panel"].orderOut_(None)
        before = ask["before"]
        if not reactivate or before is None:
            return
        before.activateWithOptions_(0)
        if not submit:
            return
        pid = int(before.processIdentifier())
        waited = [0]

        def check() -> None:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and int(front.processIdentifier()) == pid:
                ask["on_done"](text, True)
            elif waited[0] >= _ASK_WAIT_MS:
                ask["on_done"](text, False)
            else:
                waited[0] += 50
                self._clock.call_later(50, check)

        self._clock.call_later(50, check)


class MacGhost:
    """GhostPort: grey completion text right after the caret, click-through."""

    def __init__(self, font_factor: float = 0.81) -> None:
        self._factor = font_factor
        self._panel = make_panel(click_through=True)
        self._panel.setHasShadow_(False)
        self._text: str | None = None
        self._font_px = 0

    def show(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None:
        t = tokens()
        px = max(9.0, float(font_px))
        lab = NSTextField.labelWithString_(text)
        lab.setFont_(NSFont.systemFontOfSize_(px))
        lab.setTextColor_(color(t["ghost"]))
        lab.setDrawsBackground_(False)
        lab.sizeToFit()
        w = max(1.0, min(float(max_width), lab.frame().size.width + 2))
        h = lab.frame().size.height
        lab.setFrame_(NSMakeRect(0, 0, w, h))
        self._panel.setContentView_(lab)
        caret_h = round(px / self._factor)  # centred in the caret box: baselines line up
        top = int(y + (caret_h - h) // 2)
        place(self._panel, Rect(int(x), top, int(w), int(h)))
        self._panel.orderFrontRegardless()
        self._text, self._font_px = text, int(px)

    def hide(self) -> None:
        self._panel.orderOut_(None)
        self._text = None

    def shown_text(self) -> str | None:
        return self._text

    def font_px(self) -> int:
        return self._font_px

    def geometry(self) -> tuple[int, int, int, int]:
        return frame_of(self._panel)

    def monitors(self) -> list[Rect]:
        return monitors()


def frontmost_pid() -> int:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else 0

