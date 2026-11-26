"""MacOverlay: the OverlayPort in AppKit — status icon, strip, assistant panel — and MacGhost.

Everything is placed by the shared `place_status` / `place_strip` in top-left coordinates
and converted to AppKit's bottom-left space only when a panel is moved (`appkit_util.place`).
The panels never activate Glance and never take the keyboard, except the "Rewrite as…" ask
panel. Colours are `ui/theme.py`'s tokens, light or dark with the system appearance; the brand
mark, keycaps and tone glyphs are the ones the GTK overlay and the GNOME Shell extension draw
(`ui/look.py`, `ui/assets`).

Needs PyObjC (AppKit); imported only on a Mac, in glanced.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from AppKit import (
    NSAttributedString,
    NSBackgroundColorAttributeName,
    NSBezelStyleRounded,
    NSBezierPath,
    NSButton,
    NSColor,
    NSCursor,
    NSFont,
    NSFontAttributeName,
    NSFontManager,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageRep,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSMutableAttributedString,
    NSPasteboard,
    NSPasteboardTypeString,
    NSProgressIndicator,
    NSRectFillUsingOperation,
    NSScrollView,
    NSTextField,
    NSTrackingArea,
    NSView,
    NSWorkspace,
)
from Foundation import NSMakePoint, NSMakeRect, NSZeroRect

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
from glance.protocol import Rect
from glance.ui import look
from glance.ui.chip_model import ChipView
from glance.ui.hover_menu import CLOSE_MS as MENU_CLOSE_MS
from glance.ui.hover_menu import OPEN_MS as MENU_OPEN_MS
from glance.ui.hover_menu import HoverMenu
from glance.ui.panel_model import PanelView, SuggestedBox, ToneChip, suggested_box
from glance.ui.placement import Anchor, clamp_to_monitor, place_status, place_strip
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import StripView, scroll_max_h
from glance.ui.theme import glow_path, mark_path, rgba

ICON = 22
ICON_X, ICON_Y = 1, 5
ICON_BOX = (30, 28)
BADGE_C, BADGE_R = (23.0, 7.0), 7.0
MARK_PX = 18.0  # the brand mark in the icon's (invisible) click target
_SOURCE_OVER = 2  # NSCompositingOperationSourceOver
_SOURCE_ATOP = 5  # NSCompositingOperationSourceAtop
SPIN_MS = look.FRAME_MS  # the working animation's frame time, only while working
DRAWER_MIN_W = 520
GAP = 4
PAD = 10
FONT_PX = 13.0
ASK_PLACEHOLDER = "e.g. make it a bullet list"
_LINE_FIXES = 3
_ASK_WAIT_MS = 1500


def _font(bold: bool = False, size: float = FONT_PX) -> Any:
    return NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)


def _attr(text: str, fg: Any, bold: bool = False, size: float = FONT_PX) -> Any:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: _font(bold, size), NSForegroundColorAttributeName: fg})


def _mono(size: float = 11.0) -> Any:
    try:
        return NSFont.monospacedSystemFontOfSize_weight_(size, 0.0)
    except AttributeError:  # before macOS 10.15
        return NSFont.userFixedPitchFontOfSize_(size)


_MARKS: dict[tuple[float, str], Any] = {}


def mark_image(px: float, kind: str = "mark") -> Any:
    """The brand mark at `px` points, with its 1x and 2x files: AppKit draws the one for the
    screen, so it is crisp on Retina too. `kind` "glow": the soft glow behind it, bigger by
    its pad (`theme.GLOW_PAD`) on every side."""
    key = (px, kind)
    if key not in _MARKS:
        files = mark_path if kind == "mark" else glow_path
        side = px
        img = None
        for scale in (1, 2):
            rep = NSImageRep.imageRepWithContentsOfFile_(files(int(px * scale)))
            if rep is None:
                continue
            if img is None:
                mark = NSImageRep.imageRepWithContentsOfFile_(mark_path(int(px * scale)))
                k = px / mark.pixelsWide() if mark is not None else 1 / scale
                side = rep.pixelsWide() * k
                img = NSImage.alloc().initWithSize_((side, side))
            rep.setSize_((side, side))
            img.addRepresentation_(rep)
        _MARKS[key] = img if img is not None else NSImage.alloc().initWithSize_((px, px))
    return _MARKS[key]


_TINTED: dict[tuple[int, str, str], Any] = {}


def tinted(img: Any, colour: str) -> Any:
    """`img` as one flat colour (its alpha kept): the glow in the theme's `icon_glow`, the
    muted mark in grey."""
    key = (id(img), colour, "t")
    if key not in _TINTED:
        size = img.size()
        out = NSImage.alloc().initWithSize_(size)
        out.lockFocus()
        rect = NSMakeRect(0, 0, size.width, size.height)
        img.drawInRect_fromRect_operation_fraction_(rect, NSZeroRect, _SOURCE_OVER, 1.0)
        r, g, b, _a = rgba(colour)
        NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0).set()
        NSRectFillUsingOperation(rect, _SOURCE_ATOP)
        out.unlockFocus()
        _TINTED[key] = out
    return _TINTED[key]


def _mark_view(px: float) -> Any:
    view = NSImageView.alloc().initWithFrame_(NSMakeRect(0, 0, px, px))
    view.setImage_(mark_image(px))
    return view


def glyph_path(name: str, x: float, y: float, px: float) -> Any:
    """One of `look.GLYPHS`, `px` square at (x, y) in a flipped view."""
    k = px / 16
    p = NSBezierPath.bezierPath()
    for cmd, *pts in look.GLYPHS[name]:
        xy = [NSMakePoint(x + pts[i] * k, y + pts[i + 1] * k) for i in range(0, len(pts), 2)]
        if cmd == "M":
            p.moveToPoint_(xy[0])
        elif cmd == "L":
            p.lineToPoint_(xy[0])
        elif cmd == "C":
            p.curveToPoint_controlPoint1_controlPoint2_(xy[2], xy[0], xy[1])
        else:
            p.closePath()
    return p


def cgcolor(hex_colour: str) -> Any:
    from Quartz import CGColorCreateSRGB

    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return CGColorCreateSRGB(r, g, b, 1.0)


# NSTrackingArea options: entered/exited, cursor updates, while Glance is not the active app
# (it never is for the overlay), over whatever part of the view is visible.
_TRACKING = 0x01 | 0x04 | 0x80 | 0x200


def _track(view: Any) -> None:
    """Have `view` hear the pointer enter and leave (its mouseEntered_ / mouseExited_ /
    cursorUpdate_), once."""
    if getattr(view, "tracked", False):
        return
    view.tracked = True
    view.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
        NSMakeRect(0, 0, 0, 0), _TRACKING, view, None))


def _hover_cb(view: Any, name: str) -> None:
    cb = getattr(view, name, None)
    if cb is not None:
        try:
            cb()
        except Exception as e:  # noqa: BLE001
            log_event("ui_action_error", kind=type(e).__name__)


class _Flipped(NSView):
    """A view with a top-left origin, so layout reads like the GTK overlay's."""

    def isFlipped(self):
        return True


class _Card(_Flipped):
    """A rounded card: the strip, the panel, a box or chip in it (`radius`, `fill`, `border`;
    `glow`: a faint line just inside the border, the strip's; never a bloom)."""

    def drawRect_(self, _rect):
        fill = getattr(self, "fill", None)
        border = getattr(self, "border", None)
        if fill is None:
            return
        r = float(getattr(self, "radius", 10.0))
        b = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(0.5, 0.5, b.size.width - 1, b.size.height - 1), r, r)
        fill.setFill()
        path.fill()
        glow = getattr(self, "glow", None)
        if glow is not None:
            inner = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(2, 2, b.size.width - 4, b.size.height - 4), r - 1.5, r - 1.5)
            glow.setStroke()
            inner.setLineWidth_(1)
            inner.stroke()
        if border is not None:
            border.setStroke()
            path.setLineWidth_(1)
            path.stroke()


class _Glyph(_Flipped):
    """A tone glyph (`look.GLYPHS`): `glyph`, its colour `fg`."""

    def drawRect_(self, _rect):
        name, fg = getattr(self, "glyph", None), getattr(self, "fg", None)
        if not name or fg is None:
            return
        px = self.bounds().size.width
        p = glyph_path(name, 0.5, 0.5, px - 1)
        p.setLineWidth_(1.5)
        p.setLineCapStyle_(1)  # round
        p.setLineJoinStyle_(1)
        fg.setStroke()
        p.stroke()

    def acceptsFirstMouse_(self, _event):
        return True


class _Icon(_Flipped):
    """The status icon. `state` is set by the overlay; a click calls `on_click`; the
    pointer entering and leaving it, `on_enter` / `on_leave` (the hover menu)."""

    def acceptsFirstMouse_(self, _event):
        return True

    def viewDidMoveToWindow(self):
        _track(self)

    def mouseEntered_(self, _event):
        NSCursor.pointingHandCursor().set()
        _hover_cb(self, "on_enter")

    def mouseExited_(self, _event):
        NSCursor.arrowCursor().set()
        _hover_cb(self, "on_leave")

    def cursorUpdate_(self, _event):
        NSCursor.pointingHandCursor().set()

    def mouseDown_(self, _event):
        cb = getattr(self, "on_click", None)
        if cb is not None:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log_event("ui_action_error", kind=type(e).__name__)

    def drawRect_(self, _rect):
        """The brand mark alone, no disc or ring (`look.icon_look`): softer and without its
        glow when idle, its glow breathing while working, full colour with a gentle glow
        and the count badge at its top right when ready, a grey silhouette when muted."""
        view = getattr(self, "view", None)
        t = getattr(self, "tokens", None)
        if view is None or t is None:
            return
        lk = look.icon_look(view.state.value, view.spin, int(getattr(self, "phase", 0)))
        cx, cy = ICON_X + ICON / 2, ICON_Y + ICON / 2
        if lk.glow > 0:
            glow = mark_image(MARK_PX, "glow")
            side = glow.size().width
            alpha = min(1.0, rgba(t[lk.glow_token])[3] * lk.glow)
            soft = tinted(glow, t[lk.glow_token])
            soft.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                NSMakeRect(cx - side / 2, cy - side / 2, side, side), NSZeroRect,
                _SOURCE_OVER, alpha, True, None)
        if lk.arc is not None:  # working: a thin arc turning around the mark
            deg = 57.29578
            arc = NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                NSMakePoint(cx, cy), look.ARC_RADIUS, lk.arc * deg,
                (lk.arc + look.ARC_SWEEP) * deg, False)
            arc.setLineWidth_(look.ARC_WIDTH)
            arc.setLineCapStyle_(1)  # round
            color(t["accent_fill"]).setStroke()
            arc.stroke()
        mark = mark_image(MARK_PX)
        if lk.grey:
            mark = tinted(mark, t["dot_quiet"])
        mark.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(cx - MARK_PX / 2, cy - MARK_PX / 2, MARK_PX, MARK_PX), NSZeroRect,
            _SOURCE_OVER, lk.mark_alpha, True, None)
        badge = view.badge
        if badge is not None:
            dot = badge == "•"
            r = 3.5 if dot else BADGE_R - 0.8
            fill = NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(BADGE_C[0] - r, BADGE_C[1] - r, 2 * r, 2 * r))
            color(t["dot_warn" if dot else "accent_fill"]).setFill()
            fill.fill()
            if not dot:
                s = _attr(badge, color(t["on_accent"]), bold=True, size=8.5)
                size = s.size()
                s.drawAtPoint_(NSMakePoint(BADGE_C[0] - size.width / 2,
                                           BADGE_C[1] - size.height / 2))


class _AskField(NSTextField):
    pass


class _Button(NSButton):
    """A button that takes the click that reaches a panel of an inactive app (Glance never
    activates for the strip or the drawer), with the hand cursor over it."""

    def acceptsFirstMouse_(self, _event):
        return True

    def viewDidMoveToWindow(self):
        _track(self)

    def mouseEntered_(self, _event):
        NSCursor.pointingHandCursor().set()

    def mouseExited_(self, _event):
        NSCursor.arrowCursor().set()

    def cursorUpdate_(self, _event):
        NSCursor.pointingHandCursor().set()


class _MenuCard(_Card):
    """The hover menu's card: tells the overlay when the pointer enters and leaves it."""

    def viewDidMoveToWindow(self):
        _track(self)

    def mouseEntered_(self, _event):
        _hover_cb(self, "on_enter")

    def mouseExited_(self, _event):
        _hover_cb(self, "on_leave")

    def cursorUpdate_(self, _event):
        NSCursor.arrowCursor().set()


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


def _keycap(text: str, t: dict[str, str], size: float = 11.0) -> Any:
    """A keycap: a small bordered label (Alt+Enter, Esc, 1-4, the return key, Tab)."""
    lab = NSTextField.labelWithString_(text)
    lab.setFont_(_mono(size))
    lab.setTextColor_(color(t["keycap_text"]))
    lab.setAlignment_(2)  # centred
    lab.sizeToFit()
    f = lab.frame()
    lab.setFrameSize_((f.size.width + 10, f.size.height + 2))
    lab.setWantsLayer_(True)
    layer = lab.layer()
    layer.setBackgroundColor_(cgcolor(t["keycap_bg"]))
    layer.setBorderColor_(cgcolor(t["keycap_border"]))
    layer.setBorderWidth_(1.0)
    layer.setCornerRadius_(4.0)
    return lab


def _skeleton(t: dict[str, str], width: float) -> Any:
    """A loading placeholder bar (`look.SKELETON_*`)."""
    bar = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, max(8.0, width), look.SKELETON_BAR_H))
    bar.fill, bar.radius, bar.border = color(t["skeleton"]), look.SKELETON_BAR_H / 2, None
    return bar


def _loader(text: str, t: dict[str, str], width: float) -> list[Any]:
    """A small native spinner and "Checking with Claude…", muted (for `_Rows.run`)."""
    spin = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(0, 0, 12, 12))
    spin.setStyle_(1)  # spinning
    spin.setControlSize_(2)  # mini
    spin.setDisplayedWhenStopped_(False)
    spin.startAnimation_(None)
    lab = _label(text, color(t["muted"]), width - 18, lines=1, size=12)
    lab.sizeToFit()
    return [spin, lab]


def _glyph(name: str, fg: Any, px: float = 14.0) -> Any:
    view = _Glyph.alloc().initWithFrame_(NSMakeRect(0, 0, px, px))
    view.glyph, view.fg = name, fg
    return view


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
    GTK overlay's (use, more, accept, fix_all, add_dict, ignore, tone, profile,
    copy, close, guard_silence; sel_copy, sel_paste in a select-anything panel)."""

    def __init__(self, on_chip_click: Callable[[], None],
                 on_panel_action: Callable[[str, dict[str, Any]], None],
                 clock: Any, use_key: Callable[[], str] = lambda: "Alt+Return",
                 hover_menu: Callable[[], HoverMenu | None] = lambda: None) -> None:
        self._on_chip_click = on_chip_click
        self._hover_menu = hover_menu
        self._menu_view: HoverMenu | None = None
        self._menu_open: Any = None
        self._menu_close: Any = None
        self._in_icon = self._in_menu = False
        self._icon_xy: tuple[int, int] | None = None
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
        self._icon_view.on_click = self._icon_clicked
        self._icon_view.on_enter = lambda: self._menu_enter("icon")
        self._icon_view.on_leave = lambda: self._menu_leave("icon")
        self._icon.setContentView_(self._icon_view)
        self._menu = make_panel()
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
        self._hide_menu()
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
        self._icon_xy = (x, y)
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
        self._phase = (self._phase + 1) % look.FRAMES
        self._icon_view.phase = self._phase
        self._icon_view.setNeedsDisplay_(True)

    # ------------------------------------------------------------------ the hover menu

    def _icon_clicked(self) -> None:
        self._hide_menu()
        self._on_chip_click()

    def _cancel(self, attr: str) -> None:
        handle = getattr(self, attr)
        if handle is not None:
            handle.cancel()
            setattr(self, attr, None)

    def _menu_enter(self, where: str) -> None:
        if where == "icon":
            self._in_icon = True
        else:
            self._in_menu = True
        self._cancel("_menu_close")
        if where == "icon" and self._menu_view is None and self._menu_open is None:
            self._menu_open = self._clock.call_later(MENU_OPEN_MS, self._menu_open_due)

    def _menu_leave(self, where: str) -> None:
        if where == "icon":
            self._in_icon = False
            self._cancel("_menu_open")
        else:
            self._in_menu = False
        if not (self._in_icon or self._in_menu) and self._menu_close is None:
            self._menu_close = self._clock.call_later(MENU_CLOSE_MS, self._menu_close_due)

    def _menu_open_due(self) -> None:
        self._menu_open = None
        if self._in_icon and self._icon_xy is not None and self._status_view is not None:
            menu = self._hover_menu()  # None while the user types
            if menu is not None:
                self._show_menu(menu)

    def _menu_close_due(self) -> None:
        self._menu_close = None
        if not (self._in_icon or self._in_menu):
            self._hide_menu()

    def _show_menu(self, menu: HoverMenu) -> None:
        self._menu_view = menu
        t, width = self._t, 230.0
        card = _MenuCard.alloc().initWithFrame_(NSMakeRect(0, 0, width + 2 * PAD, 10))
        card.fill, card.border = color(t["surface"]), color(t["hairline"])
        card.on_enter = lambda: self._menu_enter("menu")
        card.on_leave = lambda: self._menu_leave("menu")
        rows = _Rows(card, width, top=8)
        if menu.note:
            rows.full(_label(menu.note, color(t["muted"]), width, size=12), gap=4)
        for item in menu.items:
            def run(item: Any = item) -> None:
                self._hide_menu()
                self._act(item.action, dict(item.args))

            item_b = self._link(item.label, run)
            item_b.setAttributedTitle_(_attr(item.label, color(t["body"])))
            item_b.sizeToFit()
            rows.full(item_b, gap=4)
        if menu.hint:  # "⌃⌥G to check": the chord as a keycap, the rest muted
            chord, _sep, rest = menu.hint.partition(" ")
            rows.run([_keycap(chord, t), _label(rest, color(t["muted"]), 150, lines=1,
                                                size=12)], gap=6)
        h = int(rows.y + 6)
        card.setFrame_(NSMakeRect(0, 0, width + 2 * PAD, h))
        self._menu.setContentView_(card)
        x, y = self._icon_xy or (0, 0)
        mx, my = clamp_to_monitor(x + ICON_BOX[0], y, (int(width + 2 * PAD), h),
                                  self.monitors())
        place(self._menu, Rect(mx, my, int(width + 2 * PAD), h))
        self._menu.orderFrontRegardless()

    def _hide_menu(self) -> None:
        self._cancel("_menu_open")
        self._cancel("_menu_close")
        self._in_menu = False
        self._menu_view = None
        self._menu.orderOut_(None)

    def menu_items(self) -> list[str] | None:
        """Test access: the hover menu's item labels, None while it is closed."""
        v = self._menu_view
        return None if v is None else [i.label for i in v.items]

    def _field_drawer(self) -> bool:
        return self._panel_view is not None and self._panel_anchor is not None

    def _layout(self, force: bool = False) -> None:
        self._restyle()
        mons = self.monitors()
        if self._panel_view is not None and self._panel_anchor is None:
            self._place_selection(mons)
        # While the field's panel is open it shows the corrected text itself ("Suggested"):
        # the strip is not shown on its own then.
        drawer = self._field_drawer()
        strip = self._strip_view is not None and not drawer
        if drawer:
            self._strip.orderOut_(None)
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
        if primary:  # an emerald pill: a bezel colour is not honoured on every macOS
            b.setBordered_(False)
            b.setAttributedTitle_(_attr(title, color(self._t["on_accent"]), bold=True))
            b.sizeToFit()
            f = b.frame()
            b.setFrameSize_((f.size.width + 28, max(28.0, f.size.height + 10)))
            b.setWantsLayer_(True)
            b.layer().setBackgroundColor_(cgcolor(self._t["accent_fill"]))
            b.layer().setCornerRadius_(7.0)
            return b
        b.setBezelStyle_(NSBezelStyleRounded)
        b.setFont_(_font())
        b.sizeToFit()
        return b

    def _use_chip(self, title: str) -> Any:
        """The strip's "Alt+Enter" keycap chip: emerald border and ink on the tint."""
        t = self._t
        b = _Button.buttonWithTitle_target_action_(title, self._target, "fire:")
        b.setTag_(self._target.add(lambda: self._act("use", {})))
        b.setRefusesFirstResponder_(True)
        b.setBordered_(False)
        b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            title, {NSFontAttributeName: _mono(11.5),
                    NSForegroundColorAttributeName: color(t["accent"])}))
        b.sizeToFit()
        f = b.frame()
        b.setFrameSize_((f.size.width + 14, max(20.0, f.size.height + 4)))
        b.setWantsLayer_(True)
        layer = b.layer()
        layer.setBackgroundColor_(color(t["highlight_rgba"]).CGColor())
        layer.setBorderColor_(cgcolor(t["use_border"]))
        layer.setBorderWidth_(1.0)
        layer.setCornerRadius_(5.0)
        b.setToolTip_(f"Use this ({title})")
        return b

    def _cover(self, card: Any, fn: Callable[[], None], ident: str | None = None) -> Any:
        """A clear button over the whole of `card` (a tone chip, "Rewrite as…"): the click
        and the hand cursor."""
        f = card.frame()
        b = _Button.alloc().initWithFrame_(NSMakeRect(0, 0, f.size.width, f.size.height))
        b.setTitle_("")
        b.setBordered_(False)
        b.setTransparent_(True)
        b.setTarget_(self._target)
        b.setAction_("fire:")
        b.setTag_(self._target.add(fn))
        b.setRefusesFirstResponder_(True)
        if ident:
            b.setAccessibilityIdentifier_(ident)
        card.addSubview_(b)
        return b

    def _act_button(self, title: str, action: str, primary: bool = False, **args: Any) -> Any:
        return self._button(title, lambda: self._act(action, dict(args)), primary)

    def _link(self, title: str, fn: Callable[[], None], strong: bool = False,
              muted: bool = False) -> Any:
        b = _Button.buttonWithTitle_target_action_(title, self._target, "fire:")
        b.setTag_(self._target.add(fn))
        b.setBordered_(False)
        b.setAttributedTitle_(_attr(title, color(self._t["muted" if muted else "accent"]),
                                    bold=strong, size=12.0))
        b.sizeToFit()
        b.setRefusesFirstResponder_(True)
        return b

    def _build_strip(self, width: float) -> int:
        view = self._strip_view
        assert view is not None
        t = self._t
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 40))
        warn = view.guard is not None and not view.segments
        plain = not warn and not view.segments
        card.fill = color(t["warn_bg"] if warn else t["strip"])
        card.border = color(t["warn_border"] if warn else t["hairline"] if plain
                            else t["strip_border"])
        card.glow = None if warn or plain else color(t["strip_shadow"])
        self._strip_buttons_reset()
        buttons = []
        if view.can_use:
            buttons.append(self._use_chip(look.chord_text(self._use_key(), mac=True)))
        elif view.loading is not None and view.segments:  # waits for the AI's text
            off = _keycap(look.chord_text(self._use_key(), mac=True), t)
            off.setAlphaValue_(0.45)
            buttons.append(off)
        if view.menu:
            more = self._link("⋯", lambda: self._act("more", {}), strong=True, muted=True)
            buttons.append(more)
        bw = sum(b.frame().size.width + 6 for b in buttons)
        lead = PAD + 16 + 10  # the mark, then the text
        text_w = max(80.0, width - lead - PAD - bw)
        rows = _Rows(card, text_w, top=9, left=lead)
        if view.segments:
            s = NSMutableAttributedString.alloc().init()
            for seg in view.shown:
                s.appendAttributedString_(_attr(
                    seg.text, color(t["accent"] if seg.changed else t["body"]), seg.changed,
                    size=14.0))
            scroll = view.expanded and view.compact  # "⋯": the whole corrected text
            lab = _label("", color(t["body"]), text_w, lines=0 if scroll else 5)
            lab.setAttributedStringValue_(s)
            _fit(lab, text_w)
            lab.setAccessibilityIdentifier_("glance-strip-sentence")
            if scroll:
                rows.full(self._scrolled(lab, text_w), gap=2)
            else:
                rows.full(lab, gap=2)
        if view.loading is not None:  # "Checking with Claude…": the AI's text is coming
            rows.run(_loader(view.loading, t, text_w), gap=6)
        for line in (view.summary if view.segments else None, view.meta):
            if line is not None:
                rows.full(_label(line, color(t["muted"]), text_w, size=12), gap=2)
        if view.guard is not None:
            rows.full(_label(view.guard, color(t["warn_text"]), text_w, bold=True), gap=2)
        if view.note is not None and not view.segments:
            rows.full(_label(view.note, color(t["muted"]), text_w), gap=2)
        h = max(rows.y + 9 - 2, 40.0)
        mark = _mark_view(16)
        mark.setFrameOrigin_(NSMakePoint(PAD, (h - 16) / 2))
        card.addSubview_(mark)
        x = width - PAD + 2
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

    def _chip(self, chip: ToneChip, width: float, loading: bool = False) -> Any:
        """A tone chip: its glyph and number key, its name, one line of its rewrite. A click
        selects it (emerald border and tint) and asks for its rewrite."""
        t = self._t
        line = look.card_line(chip.preview, chip.state)
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 62))
        if chip.selected:
            card.fill, card.border = color(t["highlight"]), color(t["accent_fill"])
        else:
            card.fill, card.border = color(t["card"]), color(t["card_border"])
        card.radius = 8.0
        g = _glyph(look.tone_glyph(chip.key), color(t["accent"]), 15)
        g.setFrameOrigin_(NSMakePoint(10, 8))
        card.addSubview_(g)
        if chip.hotkey is not None:
            k = _keycap(str(chip.hotkey), t)
            k.setFrameOrigin_(NSMakePoint(width - 10 - k.frame().size.width, 7))
            card.addSubview_(k)
        title = _label(chip.label, color(t["text"]), width - 20, bold=True, lines=1)
        title.setFrameOrigin_(NSMakePoint(10, 26))
        card.addSubview_(title)
        if look.card_skeleton(loading, chip.preview, chip.state):
            bar = _skeleton(t, (width - 20) * look.SKELETON_CARD[0])
            bar.setFrameOrigin_(NSMakePoint(10, 46))
            card.addSubview_(bar)
        else:
            preview = _label(line, color(t["muted"]), width - 20, lines=1, size=12)
            preview.setFrameOrigin_(NSMakePoint(10, 43))
            card.addSubview_(preview)
        cover = self._cover(card, lambda: self._act("tone", {"key": chip.key}),
                            f"card:{chip.key}")
        if chip.preview:
            cover.setToolTip_(chip.preview)
        return card

    def _rewrite_as(self, width: float) -> Any:
        """"Rewrite as… e.g. make it a bullet list" with the return key."""
        t = self._t
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 32))
        card.fill, card.border, card.radius = color(t["card"]), color(t["card_border"]), 8.0
        g = _glyph("pen", color(t["muted"]), 14)
        g.setFrameOrigin_(NSMakePoint(10, 9))
        card.addSubview_(g)
        lead = _label(look.REWRITE_LEAD, color(t["body"]), 90, lines=1)
        lead.sizeToFit()
        lead.setFrameOrigin_(NSMakePoint(32, 7))
        card.addSubview_(lead)
        x = 32 + lead.frame().size.width + 8
        k = _keycap(look.RETURN_KEY, t)
        hint = _label(ASK_PLACEHOLDER, color(t["muted"]), width - x - k.frame().size.width - 20,
                      lines=1)
        hint.setFrameOrigin_(NSMakePoint(x, 7))
        card.addSubview_(hint)
        k.setFrameOrigin_(NSMakePoint(width - 10 - k.frame().size.width, 7))
        card.addSubview_(k)
        self._cover(card, lambda: self._act("tone", {"key": "custom"}), "rewrite-as")
        return card

    def _build_drawer(self, width: float) -> int:
        view = self._panel_view
        assert view is not None
        t = self._t
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 100))
        card.fill, card.border, card.radius = color(t["surface"]), color(t["hairline"]), 12.0
        inner = width - 2 * 16
        rows = _Rows(card, inner, top=14, left=16)
        if view.mode == "selection":
            self._fill_selection(rows, view, inner)
        else:
            self._fill_drawer(rows, view, inner)
        h = rows.y + 14 - 12
        card.setFrameSize_((width, h))
        self._panel.setContentView_(card)
        return int(h)

    def _header(self, rows: _Rows, view: PanelView) -> None:
        """The brand mark, "Glance Assistant" and what is at work; who it is for; Esc Close."""
        t = self._t
        has_tones = bool(view.tone_chips)
        block = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, rows.width - 200, 36))
        mark = _mark_view(22)
        mark.setFrameOrigin_(NSMakePoint(0, 7))
        block.addSubview_(mark)
        title = _label(look.TITLE, color(t["text"]), rows.width - 232, bold=True, lines=1,
                       size=14)
        title.setFrameOrigin_(NSMakePoint(32, 0))
        block.addSubview_(title)
        sub = look.subline(view.mode, has_tones)
        line = None if has_tones else look.tone_line(view.tone, view.tone_reason)
        subline = _label(sub + (f" · {line}" if line else ""), color(t["muted"]),
                         rows.width - 232, lines=1, size=12)
        subline.setFrameOrigin_(NSMakePoint(32, 19))
        block.addSubview_(subline)
        right = []
        if view.profile_label:
            if view.mode == "selection":
                right.append(_label(f"For {view.profile_label}", color(t["muted"]), 140,
                                    lines=1, size=12))
            else:
                right.append(self._link(f"For {view.profile_label} ▾", self._toggle_profiles,
                                        muted=True))
        right.append(_keycap(look.ESC_KEY, t))
        right.append(self._link(look.CLOSE, lambda: self._act("close", {}), muted=True))
        rows.run([block], right=right, gap=8)
        rows.y += 4

    def _section(self, rows: _Rows, text: str, extra: str | None = None) -> None:
        t = self._t
        head = [_label(text, color(t["muted"]), 120, bold=True, lines=1, size=12)]
        head[0].sizeToFit()
        if extra:
            head.append(_label(extra, color(t["muted"]), rows.width - 140, lines=1, size=12))
        rows.run(head, gap=8)

    def _suggested(self, rows: _Rows, box_view: SuggestedBox) -> None:
        """"Suggested": the corrected text, or the selected tone's rewrite (its name beside
        the title, "Rewriting…" while it comes in), every changed word tinted, and
        "Apply (⌥⏎)"."""
        t = self._t
        head = [_label(look.SUGGESTED, color(t["muted"]), 120, bold=True, lines=1, size=12)]
        head[0].sizeToFit()
        if box_view.tone:
            tag = _label(box_view.tone, color(t["accent"]), 160, bold=True, lines=1, size=12)
            tag.sizeToFit()
            head.append(tag)
        right = [_label(look.card_line(None, "loading"), color(t["muted"]), 100, lines=1,
                        size=12)] if box_view.working else []
        if box_view.loading is not None:
            right = _loader(box_view.loading, t, 200)
        rows.run(head, right=right, gap=8)
        if box_view.loading is not None:  # skeleton lines until the AI's text is back
            inner = rows.width - 24
            h = len(look.SKELETON_SUGGESTED) * (look.SKELETON_BAR_H + look.SKELETON_GAP) + 13
            box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, rows.width, h))
            box.fill, box.radius, box.border = color(t["card"]), 8.0, color(t["card_border"])
            y = 10.0
            for share in look.SKELETON_SUGGESTED:
                bar = _skeleton(t, inner * share)
                bar.setFrameOrigin_(NSMakePoint(12, y))
                box.addSubview_(bar)
                y += look.SKELETON_BAR_H + look.SKELETON_GAP
            rows.full(box, gap=8)
            rows.y += 4
            return
        s = NSMutableAttributedString.alloc().init()
        if box_view.placeholder is not None:
            s.appendAttributedString_(_attr(box_view.placeholder, color(t["muted"]), size=13.0))
        for seg in box_view.segments:
            part = _attr(seg.text, color(t["text"]), size=14.0)
            if seg.changed:
                part = NSAttributedString.alloc().initWithString_attributes_(seg.text, {
                    NSFontAttributeName: _font(False, 14.0),
                    NSForegroundColorAttributeName: color(t["text"]),
                    NSBackgroundColorAttributeName: color(t["highlight"])})
            s.appendAttributedString_(part)
        inner = rows.width - 24
        lab = _label("", color(t["text"]), inner, lines=0 if box_view.scroll else 8)
        lab.setAttributedStringValue_(s)
        _fit(lab, inner)
        text: Any = self._scrolled(lab, inner) if box_view.scroll else lab
        h = min(text.frame().size.height, 200.0)
        text.setFrameSize_((inner, h))
        box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, rows.width, h + 20))
        box.fill, box.radius = color(t["card"]), 8.0
        box.border = color(t["accent_fill" if box_view.tone else "card_border"])
        text.setFrameOrigin_(NSMakePoint(12, 10))
        box.addSubview_(text)
        rows.full(box, gap=8)
        lines = [_label(x, color(t["muted"]), rows.width - 180, lines=2, size=12)
                 for x in box_view.lines]
        right = [self._act_button(look.apply_label(self._use_key(), mac=True), "use",
                                  primary=True)] if box_view.can_apply else []
        if lines or right:
            rows.run(lines, right=right)
        rows.y += 4

    def _tones(self, rows: _Rows, view: PanelView) -> None:
        """The "Tone" section: how the text reads, the chips, "Rewrite as…"."""
        cards = tuple(c for c in view.tone_chips if c.key != "custom")
        custom = any(c.key == "custom" for c in view.tone_chips)
        if not (cards or custom):
            return
        self._section(rows, look.TONE, look.tone_line(view.tone, view.tone_reason))
        if cards:
            n = len(cards)
            w = (rows.width - 8 * (n - 1)) / n
            rows.run([self._chip(c, w, view.loading) for c in cards], gap=8)
        if custom:
            rows.full(self._rewrite_as(rows.width), gap=8)
        rows.y += 4

    def _act_link(self, title: str, action: str, strong: bool = False, **args: Any) -> Any:
        return self._link(title, lambda: self._act(action, dict(args)), strong,
                          muted=not strong)

    def _rule(self, rows: _Rows) -> None:
        line = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, rows.width, 1))
        line.setWantsLayer_(True)
        line.layer().setBackgroundColor_(cgcolor(self._t["hairline"]))
        rows.full(line, gap=10)

    def _fill_drawer(self, rows: _Rows, view: PanelView, inner: float) -> None:
        t = self._t
        if view.guard:
            rows.run([_label(view.guard, color(t["warn_text"]), inner - 260, lines=2)],
                     right=[self._act_link("Don't warn about this here", "guard_silence",
                                           scope="app"),
                            self._act_link("Never", "guard_silence", scope="global")])
        self._header(rows, view)
        if view.profile_label and view.profiles and self._profiles_open:
            rows.run([self._act_button(label, "profile", name=name)
                      for name, label in view.profiles])
        strip = self._strip_view  # shown here while the panel is open, not on its own
        box = suggested_box(view, strip)
        suggested = box is not None
        if box is not None:
            self._suggested(rows, box)
        elif strip is not None and strip.note and not view.tone:
            rows.full(_label(strip.note, color(t["muted"]), inner, size=12))
        self._tones(rows, view)
        if view.banner:
            rows.full(_label(view.banner, color(t["warn_text"]), inner))
        if view.copy_offer is not None:
            rewrite = view.preview_state is not None
            rows.run([_label("The rewrite was not typed into the field." if rewrite
                             else "The change could not be applied here.",
                             color(t["muted"]), inner - 140, lines=2)],
                     right=[self._act_button("Copy rewrite" if rewrite else "Copy instead",
                                             "copy")])
        if view.issues and not view.loading:  # the AI's list replaces the quick one
            self._rule(rows)
            n = len(view.issues)
            line = [self._link(look.fixes_label(n), self._toggle_details, muted=True)]
            for row in [r for r in view.issues if r.replacements][:_LINE_FIXES]:
                flagged = (row.flagged or row.excerpt).replace("\n", " ")
                line.append(self._act_link(f"{flagged} → {row.replacements[0] or '(remove)'}",
                                           "accept", issue_id=row.issue_id, choice=0))
            right = [self._act_link(look.FIX_ALL, "fix_all", strong=True)] \
                if view.can_fix_all else []
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
        elif not (view.banner or view.copy_offer is not None or view.tone_chips or view.guard
                  or suggested):
            rows.full(_label("Nothing to fix here.", color(t["muted"]), inner, size=12))

    def _fill_selection(self, rows: _Rows, view: PanelView, inner: float) -> None:
        t = self._t
        self._header(rows, view)
        self._tones(rows, view)
        if view.preview is not None:
            self._section(rows, "Rewrite")
            s = NSMutableAttributedString.alloc().init()
            for op in view.preview:
                if op.kind == "del":
                    continue
                ins = op.kind == "ins"
                s.appendAttributedString_(_attr(op.text, color(t["accent"] if ins
                                                               else t["body"]), ins))
            lab = _label("", color(t["body"]), inner - 20, lines=8)
            lab.setAttributedStringValue_(s)
            _fit(lab, inner - 20)
            box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, inner,
                                                          lab.frame().size.height + 16))
            box.fill, box.border, box.radius = color(t["card"]), color(t["card_border"]), 8.0
            lab.setFrameOrigin_(NSMakePoint(10, 8))
            box.addSubview_(lab)
            rows.full(box)
        if view.preview_state == "loading":
            rows.full(_label("Rewriting…", color(t["muted"]), inner, size=12))
        if view.banner:
            rows.full(_label(view.banner, color(t["warn_text"]), inner))
        for row in view.issues[:8]:
            rows.full(_label(row.excerpt.replace("\n", " "), color(t["text"]), inner), gap=2)
            if row.message:
                rows.full(_label(row.message, color(t["muted"]), inner, size=12), gap=4)
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
        """The completion in emerald italics, then a small "Tab" keycap (Tab takes it)."""
        t = tokens()
        px = max(9.0, float(font_px))
        lab = NSTextField.labelWithString_(text)
        font = NSFont.systemFontOfSize_(px)
        lab.setFont_(NSFontManager.sharedFontManager().convertFont_toHaveTrait_(font, 1))
        lab.setTextColor_(color(t["ghost"]))
        lab.setDrawsBackground_(False)
        lab.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
        lab.sizeToFit()
        key = _keycap(look.ACCEPT_KEY, t, size=max(8.0, round(px * 0.7)))
        kw, kh = key.frame().size.width, key.frame().size.height
        h = max(lab.frame().size.height, kh)
        tw = max(1.0, min(lab.frame().size.width + 2, float(max_width) - kw - 6))
        lab.setFrame_(NSMakeRect(0, (h - lab.frame().size.height) / 2, tw,
                                 lab.frame().size.height))
        w = tw + 6 + kw
        box = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        box.addSubview_(lab)
        key.setFrameOrigin_(NSMakePoint(tw + 6, (h - kh) / 2))
        box.addSubview_(key)
        self._panel.setContentView_(box)
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

