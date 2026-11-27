"""Glance Shot on macOS: the region picker, the capture, the bar and the image pasteboard.

The picker is a borderless, non-activating panel over every screen (it becomes key only to
hear Enter, Space and Esc; Glance never becomes the active app). It follows the shared rules
(`glance.shot.picker`). The capture is ScreenCaptureKit's `SCScreenshotManager` (macOS 14+),
else `CGWindowListCreateImage`, of what is below the picker: in memory, never a file. Both
need the Screen Recording permission, asked the first time Glance Shot is used (macOS shows
its own prompt once; the user switches it on in System Settings).

Needs PyObjC (AppKit, Quartz; ScreenCaptureKit where present); imported only on a Mac.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import objc
from AppKit import (
    NSBezierPath,
    NSColor,
    NSCursor,
    NSPasteboard,
    NSScreenSaverWindowLevel,
    NSScrollView,
)
from Foundation import NSData, NSMakePoint, NSMakeRect

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
from glance.platform.macos.overlay import (
    _attr,
    _Button,
    _Card,
    _Flipped,
    _keycap,
    _label,
    _mark_view,
    _skeleton,
    cgcolor,
)
from glance.protocol import Rect
from glance.shot import bar_model as bm
from glance.shot import geometry as geo
from glance.shot.controller import PickResult
from glance.shot.image import Shot, png_size
from glance.shot.picker import HINT, Picker
from glance.ui import look
from glance.ui.ports import WindowInfo

BAR_W = 460.0  # the card
CARD_PAD = 16.0
HEADER_H = 34.0
TAB_ROW_H = 34.0
TAB_PAD = 3.0
TAB_GAP = 2.0
BODY_PAD = 12.0
BODY_MIN_H = 132.0  # the body keeps at least this height: the card barely resizes per step
RESULT_MAX_H = 260.0  # longer text scrolls
FOOT_H = 32.0
FRAME_PAD = 3  # the region's outline sits just outside it: the shot itself stays clear
DIM = 0.45
PNG_TYPE = "public.png"
NO_PERMISSION = ("Glance Shot needs Screen Recording",
                 ("Turn Glance on in System Settings → Privacy & Security → Screen & System "
                  "Audio Recording, then take the shot again."))


# ---------------------------------------------------------------------- permission


def screen_recording_allowed(prompt: bool) -> bool:
    """Whether Glance may read the screen; with `prompt`, macOS asks the user (once)."""
    try:
        import Quartz
    except ImportError:
        return False
    try:
        if Quartz.CGPreflightScreenCaptureAccess():
            return True
        if prompt:
            Quartz.CGRequestScreenCaptureAccess()
    except AttributeError:  # before macOS 10.15 there is no such permission
        return True
    return False


# ---------------------------------------------------------------------- windows and capture


def window_list() -> list[WindowInfo]:
    """On-screen normal windows (layer 0), topmost first, with their frames (top-left)."""
    import Quartz

    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    out = []
    for info in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or ():
        if int(info.get("kCGWindowLayer", 1)) != 0:
            continue
        b = info.get("kCGWindowBounds") or {}
        try:
            rect = Rect(round(b["X"]), round(b["Y"]), round(b["Width"]), round(b["Height"]))
        except (KeyError, TypeError):
            rect = None
        out.append(WindowInfo(str(info.get("kCGWindowOwnerName") or ""),
                              str(info.get("kCGWindowName") or ""),
                              int(info.get("kCGWindowOwnerPID") or 0),
                              int(info.get("kCGWindowNumber") or 0), rect))
    return out


def png_of(cgimage: Any) -> bytes | None:
    from AppKit import NSBitmapImageRep

    rep = NSBitmapImageRep.alloc().initWithCGImage_(cgimage)
    data = rep.representationUsingType_properties_(4, {}) if rep is not None else None  # PNG
    return bytes(data) if data is not None else None


def capture_below(region: Rect, below_window: int) -> bytes | None:
    """`CGWindowListCreateImage` of what is on screen in `region`, under `below_window`."""
    import Quartz

    rect = Quartz.CGRectMake(region.x, region.y, region.w, region.h)
    opts = (Quartz.kCGWindowListOptionOnScreenBelowWindow if below_window
            else Quartz.kCGWindowListOptionOnScreenOnly)
    img = Quartz.CGWindowListCreateImage(rect, opts, below_window or Quartz.kCGNullWindowID,
                                         Quartz.kCGWindowImageBestResolution)
    return png_of(img) if img is not None else None


def capture_sck(region: Rect, exclude: list[int],
                done: Callable[[bytes | None], None]) -> bool:
    """ScreenCaptureKit (macOS 14+): `done(png)` later, from any thread. False when it is
    not there (the caller uses `capture_below`)."""
    try:
        import ScreenCaptureKit as SCK
    except ImportError:
        return False
    if not hasattr(SCK, "SCScreenshotManager"):
        return False

    def content_ready(content: Any, error: Any) -> None:
        try:
            if content is None:
                done(None)
                return
            displays = list(content.displays() or ())
            display = next((d for d in displays if geo.overlaps(region, Rect(
                round(d.frame().origin.x), round(d.frame().origin.y),
                round(d.frame().size.width), round(d.frame().size.height)))), None)
            if display is None:
                done(None)
                return
            skip = [w for w in content.windows() or () if int(w.windowID()) in exclude]
            flt = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, skip)
            cfg = SCK.SCStreamConfiguration.alloc().init()
            f = display.frame()
            local = (region.x - f.origin.x, region.y - f.origin.y, region.w, region.h)
            cfg.setSourceRect_(((local[0], local[1]), (local[2], local[3])))
            scale = float(getattr(flt, "pointPixelScale", lambda: 2.0)() or 2.0)
            cfg.setWidth_(int(region.w * scale))
            cfg.setHeight_(int(region.h * scale))
            cfg.setShowsCursor_(False)
            SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                flt, cfg, lambda img, err: done(png_of(img) if img is not None else None))
        except Exception as e:  # noqa: BLE001 - any failure: no image, never a crash
            log_event("shot_sck_failed", kind=type(e).__name__)
            done(None)

    SCK.SCShareableContent.getShareableContentWithCompletionHandler_(content_ready)
    return True


# ---------------------------------------------------------------------- the picker view


class _PickerView(_Flipped):
    """Draws the dimmed screen, the region and its size label; hears the pointer and keys.
    `owner` is the MacShot; coordinates are converted to global top-left by `origin`."""

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, _event):
        return True

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.crosshairCursor())

    @objc.python_method
    def _global(self, event: Any) -> tuple[int, int]:
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        ox, oy = self.origin
        return round(ox + p.x), round(oy + p.y)

    def mouseDown_(self, event):
        self.owner.picker_event("press", *self._global(event))

    def mouseDragged_(self, event):
        self.owner.picker_event("motion", *self._global(event))

    def mouseMoved_(self, event):
        self.owner.picker_event("motion", *self._global(event))

    def mouseUp_(self, event):
        self.owner.picker_event("release", *self._global(event))

    def rightMouseDown_(self, _event):
        self.owner.picker_key("Escape")

    def keyDown_(self, event):
        code = int(event.keyCode())
        name = {53: "Escape", 36: "Return", 76: "Return", 49: "space"}.get(code)
        if name:
            self.owner.picker_key(name)

    def drawRect_(self, _rect):
        p = self.owner.picker
        if p is None:
            return
        t = self.owner.t
        ox, oy = self.origin
        b = self.bounds()
        lit = p.selection or p.hover_window()
        NSColor.colorWithSRGBRed_green_blue_alpha_(0.03, 0.05, 0.07, DIM).setFill()
        path = NSBezierPath.bezierPathWithRect_(b)
        if lit is not None:
            path.appendBezierPathWithRect_(NSMakeRect(lit.x - ox, lit.y - oy, lit.w, lit.h))
            path.setWindingRule_(1)  # even-odd: the region stays clear
        path.fill()
        if lit is not None:
            color(t["accent_fill"]).setStroke()
            frame = NSBezierPath.bezierPathWithRect_(
                NSMakeRect(lit.x - ox + 1, lit.y - oy + 1, lit.w - 2, lit.h - 2))
            frame.setLineWidth_(2.0 if p.selection is not None else 1.5)
            if p.selection is None:
                frame.setLineDash_count_phase_([6.0, 4.0], 2, 0.0)
            frame.stroke()
        text = p.label() or HINT
        attr = _attr(text, color(t["text"]), size=12.0 if p.label() else 13.0)
        size = attr.size()
        w, h = size.width + 20, size.height + 10
        if p.label():
            x, y = p.label_at((round(w), round(h)))
        else:
            mon = geo.monitor_at(*p.pointer, p.monitors) or p.screen
            x, y = mon.x + (mon.w - w) / 2, mon.y + 28
        pill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(x - ox, y - oy, w, h), h / 2, h / 2)
        color(t["surface"], 0.94).setFill()
        pill.fill()
        color(t["hairline"]).setStroke()
        pill.stroke()
        attr.drawAtPoint_(NSMakePoint(x - ox + 10, y - oy + 5))


class _Outline(_Flipped):
    """The captured region's outline: a 2px ring in `ink`, clear inside."""

    def drawRect_(self, _rect):
        ink = getattr(self, "ink", None)
        if ink is None:
            return
        b = self.bounds()
        ink.setStroke()
        ring = NSBezierPath.bezierPathWithRect_(
            NSMakeRect(1, 1, b.size.width - 2, b.size.height - 2))
        ring.setLineWidth_(2.0)
        ring.stroke()


# ---------------------------------------------------------------------- MacShot


class MacShot:
    """ShotUiPort on macOS. `post(fn)` runs `fn` on the main thread (capture callbacks
    arrive on others)."""

    def __init__(self, on_action: Callable[[str], None],
                 post: Callable[[Callable[[], None]], None],
                 notify: Callable[[str, str], None] = lambda _s, _b: None) -> None:
        self._act = on_action
        self._post = post
        self._notify = notify
        self._target = Target.alloc().init()
        self.t: dict[str, str] = tokens()
        self.picker: Picker | None = None
        self._on_done: Callable[[PickResult | None], None] | None = None
        self._windows: list[WindowInfo] = []
        self._pick_panel: Any = None
        self._view: Any = None
        self._bar = make_panel()
        self._bar_key: Any = None
        self._bar_h = 0
        self._region: Rect | None = None
        self._frame: Any = None  # the region's outline, made on first use
        self._frame_rect: Rect | None = None
        self._frame_ink: str | None = None

    # ------------------------------------------------------------------ the picker

    def pick(self, on_done: Callable[[PickResult | None], None]) -> None:
        self.cancel_pick()
        self._on_done = on_done
        if not screen_recording_allowed(prompt=True):
            log_event("shot_no_permission")
            self._notify(*NO_PERMISSION)
            self._finish(None)
            return
        self.t = tokens()
        mons = monitors(visible=False)
        try:
            self._windows = window_list()
        except Exception as e:  # noqa: BLE001
            log_event("shot_windows_failed", kind=type(e).__name__)
            self._windows = []
        self.picker = Picker(mons, [w.rect for w in self._windows if w.rect is not None])
        screen = self.picker.screen
        panel = make_panel(can_key=True)
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setHasShadow_(False)
        panel.setAcceptsMouseMovedEvents_(True)
        place(panel, screen)
        view = _PickerView.alloc().initWithFrame_(NSMakeRect(0, 0, screen.w, screen.h))
        view.owner = self
        view.origin = (screen.x, screen.y)
        panel.setContentView_(view)
        panel.on_cancel = lambda: self.picker_key("Escape")
        panel.orderFrontRegardless()
        panel.makeKeyWindow()
        panel.makeFirstResponder_(view)
        self._pick_panel, self._view = panel, view
        log_event("shot_picker_open", monitors=len(mons), windows=len(self._windows))

    def picker_event(self, kind: str, x: int, y: int) -> None:
        p = self.picker
        if p is None:
            return
        if kind == "press":
            p.press(x, y)
        elif kind == "motion":
            p.motion(x, y)
        else:
            out = p.release(x, y)
            if out is not None:
                self._end(out.region)
                return
        self._view.setNeedsDisplay_(True)

    def picker_key(self, name: str) -> None:
        p = self.picker
        if p is None:
            return
        out = p.key(name)
        if out is not None:
            self._end(out.region)
        elif self._view is not None:
            self._view.setNeedsDisplay_(True)

    def _end(self, region: Rect | None) -> None:
        windows = list(self._windows)
        number = int(self._pick_panel.windowNumber()) if self._pick_panel is not None else 0
        if region is None:
            self._close_picker()
            self._finish(None)
            return

        def got(png: bytes | None) -> None:
            def main() -> None:
                self._close_picker()
                if png is None:
                    log_event("shot_capture_failed")
                    self._finish(None)
                    return
                try:
                    w, h = png_size(png)
                except ValueError:
                    self._finish(None)
                    return
                self._finish(PickResult(Shot(png, w, h, region), tuple(windows)))

            self._post(main)

        # The picker is hidden from the capture (excluded / below it), then closed.
        if self._pick_panel is not None:
            self._pick_panel.setAlphaValue_(0.0)
        if not capture_sck(region, [number], got):
            got(capture_below(region, number))

    def _finish(self, result: PickResult | None) -> None:
        done, self._on_done = self._on_done, None
        if done is not None:
            done(result)

    def _close_picker(self) -> None:
        if self._pick_panel is not None:
            self._pick_panel.orderOut_(None)
        self._pick_panel = self._view = None
        self.picker = None
        self._windows = []

    def cancel_pick(self) -> None:
        if self._pick_panel is not None or self.picker is not None:
            self._close_picker()
            self._finish(None)

    # ------------------------------------------------------------------ the card

    def _button(self, b: bm.ShotButton) -> Any:
        """A result action or a notice button: the primary one an emerald pill, the others
        outlined."""
        t = self.t
        btn = self._plain_button(b)
        primary = b.primary and not b.muted
        fg = t["on_accent"] if primary else t["muted"] if b.muted else t["text"]
        btn.setAttributedTitle_(_attr(b.label, color(fg), bold=primary, size=12.5))
        btn.sizeToFit()
        f = btn.frame()
        btn.setFrameSize_((f.size.width + 24, 30.0))
        layer = btn.layer()
        layer.setCornerRadius_(7.0)
        if primary:
            layer.setBackgroundColor_(cgcolor(t["accent_fill"]))
        else:
            if not b.muted:
                layer.setBackgroundColor_(cgcolor(t["card"]))
            layer.setBorderColor_(cgcolor(t["card_border"]))
            layer.setBorderWidth_(1.0)
        return btn

    def _plain_button(self, b: bm.ShotButton) -> Any:
        btn = _Button.buttonWithTitle_target_action_(b.label, self._target, "fire:")
        btn.setTag_(self._target.add(lambda: self._act(b.key)))
        btn.setRefusesFirstResponder_(True)
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        if b.tip:
            btn.setToolTip_(b.tip)
        btn.setAccessibilityIdentifier_(f"shot:{b.key}")
        return btn

    def _tool(self, b: bm.ShotButton) -> Any:
        """Copy image, Save: small outlined buttons in the header."""
        t = self.t
        btn = self._plain_button(b)
        btn.setAttributedTitle_(_attr(b.label, color(t["text"]), size=12.5))
        btn.sizeToFit()
        f = btn.frame()
        btn.setFrameSize_((f.size.width + 18, 26.0))
        layer = btn.layer()
        layer.setCornerRadius_(7.0)
        layer.setBorderColor_(cgcolor(t["card_border"]))
        layer.setBorderWidth_(1.0)
        return btn

    def _tabs(self, view: bm.ShotBarView, width: float) -> Any:
        """The tab row: one rounded group, equal widths, the selected tab tinted."""
        t = self.t
        tabs = [b for b in view.buttons if b.key != "close"]
        group = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, TAB_ROW_H))
        group.fill, group.border, group.radius = color(t["group"]), color(t["card_border"]), 9.0
        if not tabs:
            return group
        inner = width - 2 * TAB_PAD
        tw = (inner - TAB_GAP * (len(tabs) - 1)) / len(tabs)
        x = TAB_PAD
        for b in tabs:
            btn = self._plain_button(b)
            fg = t["accent"] if b.selected else t["muted"]
            btn.setAttributedTitle_(_attr(b.label, color(fg), bold=b.selected, size=13.0))
            btn.setFrame_(NSMakeRect(x, TAB_PAD, tw, TAB_ROW_H - 2 * TAB_PAD))
            layer = btn.layer()
            layer.setCornerRadius_(7.0)
            if b.selected:
                layer.setBackgroundColor_(color(t["highlight_rgba"]).CGColor())
            if b.muted:
                btn.setAlphaValue_(0.5)
            group.addSubview_(btn)
            x += tw + TAB_GAP
        return group

    def _body(self, view: bm.ShotBarView, width: float) -> Any:
        """The hint, the notice, or the result, in one box of at least BODY_MIN_H."""
        t = self.t
        inner = width - 2 * BODY_PAD
        views: list[tuple[Any, float]] = []  # (view, gap after it)
        fill, border = t["card"], t["card_border"]
        if view.notice is not None:
            fill, border = t["warn_bg"], t["warn_border"]
            views.append((_label(view.notice.text, color(t["warn_text"]), inner, size=13.0), 0))
        elif view.result is not None:
            views.extend(self._result(view.result, inner))
        else:
            views.append((_label(view.hint or bm.HINT, color(t["text"]), inner, bold=True,
                                 size=13.5), 6))
            views.append((_label(bm.HINT_SUB, color(t["muted"]), inner, size=12.0), 0))
        box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, BODY_MIN_H))
        box.fill, box.border, box.radius = color(fill), color(border), 8.0
        y = BODY_PAD
        for v, gap in views:
            v.setFrameOrigin_(NSMakePoint(BODY_PAD, y))
            box.addSubview_(v)
            y += v.frame().size.height + gap
        box.setFrameSize_((width, max(BODY_MIN_H, y + BODY_PAD)))
        box.setAccessibilityIdentifier_("shot:body")
        return box

    def _result(self, r: bm.ShotResult, inner: float) -> list[tuple[Any, float]]:
        t = self.t
        head = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, inner, 18))
        title = _label(r.title, color(t["muted"]), inner, bold=True, lines=1, size=12.0)
        title.sizeToFit()
        title.setFrameOrigin_(NSMakePoint(0, 0))
        head.addSubview_(title)
        if r.state == "failed":
            why = _label(r.note or "That did not work. Try again", color(t["warn_text"]), inner,
                         size=12.5)
            return [(head, 8), (why, 0)]
        if r.note:
            room = inner - title.frame().size.width - 12
            note = _label(r.note, color(t["muted"]), room, lines=1, size=12.0)
            note.setAlignment_(2)  # right
            note.setFrame_(NSMakeRect(inner - room, 0, room, title.frame().size.height))
            head.addSubview_(note)
        out: list[tuple[Any, float]] = [(head, 8)]
        if r.text is not None:
            text = _label(r.text, color(t["text"]), inner - 14, size=13.5)
            full = text.frame().size.height
            th = min(RESULT_MAX_H, full)
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, inner, th))
            scroll.setHasVerticalScroller_(full > RESULT_MAX_H)
            scroll.setAutohidesScrollers_(True)
            scroll.setDrawsBackground_(False)
            scroll.setBorderType_(0)  # NSNoBorder
            doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, inner - 14, full))
            doc.addSubview_(text)
            scroll.setDocumentView_(doc)
            out.append((scroll, 0))
        elif r.state == "loading":
            for share in look.SKELETON_SUGGESTED:  # skeleton lines, the answer's shape
                out.append((_skeleton(t, inner * share), look.SKELETON_GAP + 4))
        return out

    def _footer(self, view: bm.ShotBarView, width: float) -> Any:
        """The notice's buttons or the result's actions on the left; the status on the
        right."""
        t = self.t
        row = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, width, FOOT_H))
        actions: tuple[bm.ShotButton, ...] = ()
        if view.notice is not None:
            actions = view.notice.buttons
        elif view.result is not None and view.result.text is not None:
            actions = view.result.actions
        x = 0.0
        for b in actions:
            btn = self._button(b)
            btn.setFrameOrigin_(NSMakePoint(x, (FOOT_H - btn.frame().size.height) / 2))
            row.addSubview_(btn)
            x += btn.frame().size.width + 6
        if view.banner:
            room = width - x - 8
            ink = t["warn_text"] if view.warn else t["accent"]
            status = _label(view.banner, color(ink), max(40.0, room), lines=1, size=12.5)
            status.setAlignment_(2)  # right
            sh = status.frame().size.height
            status.setFrame_(NSMakeRect(width - max(40.0, room), (FOOT_H - sh) / 2,
                                        max(40.0, room), sh))
            status.setToolTip_(view.banner)
            status.setAccessibilityIdentifier_("shot:status")
            row.addSubview_(status)
        return row

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None:
        self._region = region
        self.t = t = tokens()
        key = (view, region, t["surface"])
        if key != self._bar_key:
            self._bar_key = key
            self._target.reset()
            self._bar_h = self._build(view)
        h = self._bar_h
        x, y = geo.bar_position(region, (round(BAR_W), h), monitors())
        # One frame change, content already in place: the card does not flicker; on the
        # same side its top-left stays put and it only grows down.
        place(self._bar, Rect(x, y, round(BAR_W), h))
        self._bar.invalidateShadow()
        self._show_frame(region)
        if not self._bar.isVisible():
            self._bar.orderFrontRegardless()

    def _build(self, view: bm.ShotBarView) -> int:
        t = self.t
        inner = BAR_W - 2 * CARD_PAD
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, BAR_W, 10))
        card.fill, card.border, card.radius = color(t["surface"]), color(t["hairline"]), 12.0
        # the header: logo, "Glance Shot", the size; Copy image, Save, Esc Close
        y = 14.0
        mark = _mark_view(20.0)
        mark.setFrameOrigin_(NSMakePoint(CARD_PAD, y + 6))
        card.addSubview_(mark)
        title = _label(bm.TITLE, color(t["text"]), 160, bold=True, lines=1, size=14.0)
        title.setFrameOrigin_(NSMakePoint(CARD_PAD + 28, y))
        card.addSubview_(title)
        size = _label(view.size, color(t["muted"]), 160, lines=1, size=12.0)
        size.setFrameOrigin_(NSMakePoint(CARD_PAD + 28, y + 18))
        card.addSubview_(size)
        esc = _keycap(look.ESC_KEY, t)
        close = _Button.buttonWithTitle_target_action_(look.CLOSE, self._target, "fire:")
        close.setTag_(self._target.add(lambda: self._act("close")))
        close.setBordered_(False)
        close.setRefusesFirstResponder_(True)
        close.setAttributedTitle_(_attr(look.CLOSE, color(t["muted"]), size=12.0))
        close.sizeToFit()
        close.setAccessibilityIdentifier_("shot:close")
        mid = y + HEADER_H / 2
        cx = BAR_W - CARD_PAD - close.frame().size.width
        close.setFrameOrigin_(NSMakePoint(cx, mid - close.frame().size.height / 2))
        ex = cx - esc.frame().size.width - 5
        esc.setFrameOrigin_(NSMakePoint(ex, mid - esc.frame().size.height / 2))
        card.addSubview_(esc)
        card.addSubview_(close)
        x = ex - 12
        for b in reversed(view.tools):
            tool = self._tool(b)
            x -= tool.frame().size.width
            tool.setFrameOrigin_(NSMakePoint(x, mid - tool.frame().size.height / 2))
            card.addSubview_(tool)
            x -= 8
        y += HEADER_H + 12
        tabs = self._tabs(view, inner)
        tabs.setFrameOrigin_(NSMakePoint(CARD_PAD, y))
        card.addSubview_(tabs)
        y += TAB_ROW_H + 12
        body = self._body(view, inner)
        body.setFrameOrigin_(NSMakePoint(CARD_PAD, y))
        card.addSubview_(body)
        y += body.frame().size.height + 12
        foot = self._footer(view, inner)
        foot.setFrameOrigin_(NSMakePoint(CARD_PAD, y))
        card.addSubview_(foot)
        y += FOOT_H + CARD_PAD - 2
        card.setFrameSize_((BAR_W, y))
        self._bar.setContentView_(card)
        return round(y)

    def ask_point(self) -> tuple[int, int] | None:
        """Where Ask…'s one-line entry goes: over the card's body (top-left space)."""
        if not self._bar.isVisible():
            return None
        x, y, _w, _h = frame_of(self._bar)
        return x + 16, y + 104

    # ------------------------------------------------------------------ the region outline

    def _show_frame(self, region: Rect) -> None:
        """A 2px accent ring just outside the region while its card is up; clicks go
        through it."""
        p = FRAME_PAD
        r = Rect(region.x - p, region.y - p, region.w + 2 * p, region.h + 2 * p)
        if self._frame is None:
            self._frame = make_panel(click_through=True)
            self._frame.setHasShadow_(False)
        if self._frame_rect != r or self._frame_ink != self.t["accent_fill"]:
            self._frame_rect, self._frame_ink = r, self.t["accent_fill"]
            ring = _Outline.alloc().initWithFrame_(NSMakeRect(0, 0, r.w, r.h))
            ring.ink = color(self.t["accent_fill"])
            self._frame.setContentView_(ring)
            place(self._frame, r)
        if not self._frame.isVisible():
            self._frame.orderFrontRegardless()

    def _hide_frame(self) -> None:
        if self._frame is not None:
            self._frame.orderOut_(None)
        self._frame_rect = None

    def frame_geometry(self) -> tuple[int, int, int, int] | None:
        f = self._frame
        return frame_of(f) if f is not None and f.isVisible() else None

    def hide_bar(self) -> None:
        self._hide_frame()
        self._bar.orderOut_(None)
        self._bar_key = None
        self._region = None

    def bar_geometry(self) -> tuple[int, int, int, int] | None:
        return frame_of(self._bar) if self._bar.isVisible() else None

    # ------------------------------------------------------------------ pasteboard

    def copy_image(self, png: bytes) -> bool:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        return bool(pb.setData_forType_(NSData.dataWithBytes_length_(png, len(png)), PNG_TYPE))

    def close(self) -> None:
        self.cancel_pick()
        self.hide_bar()

