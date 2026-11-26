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
    PAD,
    _attr,
    _Button,
    _Card,
    _Flipped,
    _keycap,
    _label,
    _mark_view,
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

BAR_W = 560.0
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
        self._region: Rect | None = None

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

    # ------------------------------------------------------------------ the bar

    def _button(self, b: bm.ShotButton) -> Any:
        t = self.t
        btn = _Button.buttonWithTitle_target_action_(b.label, self._target, "fire:")
        btn.setTag_(self._target.add(lambda: self._act(b.key)))
        btn.setRefusesFirstResponder_(True)
        btn.setBordered_(False)
        primary = b.primary and not b.muted
        fg = t["on_accent"] if primary else t["muted"] if b.muted else t["text"]
        btn.setAttributedTitle_(_attr(b.label, color(fg), bold=primary, size=12.5))
        btn.sizeToFit()
        f = btn.frame()
        btn.setFrameSize_((f.size.width + 22, max(26.0, f.size.height + 8)))
        btn.setWantsLayer_(True)
        layer = btn.layer()
        layer.setCornerRadius_(7.0)
        if primary:
            layer.setBackgroundColor_(cgcolor(t["accent_fill"]))
        else:
            if not b.muted:
                layer.setBackgroundColor_(cgcolor(t["card"]))
            layer.setBorderColor_(cgcolor(t["card_border"]))
            layer.setBorderWidth_(1.0)
        if b.tip:
            btn.setToolTip_(b.tip)
        btn.setAccessibilityIdentifier_(f"shot:{b.key}")
        return btn

    def _box(self, fill: str, border: str, width: float, views: list[Any]) -> Any:
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 10))
        card.fill, card.border, card.radius = color(fill), color(border), 9.0
        y = 8.0
        for v in views:
            v.setFrameOrigin_(NSMakePoint(10, y))
            card.addSubview_(v)
            y += v.frame().size.height + 6
        card.setFrameSize_((width, y + 2))
        return card

    def _row(self, buttons: list[Any]) -> Any:
        row = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, BAR_W - 2 * PAD, 10))
        x = h = 0.0
        for b in buttons:
            b.setFrameOrigin_(NSMakePoint(x, 0))
            row.addSubview_(b)
            x += b.frame().size.width + 6
            h = max(h, b.frame().size.height)
        row.setFrameSize_((BAR_W - 2 * PAD, h))
        return row

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None:
        self._region = region
        self.t = t = tokens()
        key = (view, region, t["surface"])
        if key != self._bar_key:
            self._bar_key = key
            self._target.reset()
            self._build(view)
        w, h = self._bar.frame().size.width, self._bar.frame().size.height
        x, y = geo.bar_position(region, (round(w), round(h)), monitors())
        place(self._bar, Rect(x, y, round(w), round(h)))
        self._bar.orderFrontRegardless()

    def _build(self, view: bm.ShotBarView) -> None:
        t = self.t
        inner = BAR_W - 2 * PAD
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, BAR_W, 10))
        card.fill, card.border, card.radius = color(t["surface"]), color(t["hairline"]), 12.0
        y = PAD + 4
        mark = _mark_view(20.0)
        mark.setFrameOrigin_(NSMakePoint(PAD + 4, y + 4))
        card.addSubview_(mark)
        title = _label(bm.TITLE, color(t["text"]), 200, bold=True, lines=1, size=14.0)
        title.setFrameOrigin_(NSMakePoint(PAD + 32, y))
        card.addSubview_(title)
        size = _label(view.size, color(t["muted"]), 200, lines=1, size=12.0)
        size.setFrameOrigin_(NSMakePoint(PAD + 32, y + 18))
        card.addSubview_(size)
        esc = _keycap(look.ESC_KEY, t)
        close = _Button.buttonWithTitle_target_action_(look.CLOSE, self._target, "fire:")
        close.setTag_(self._target.add(lambda: self._act("close")))
        close.setBordered_(False)
        close.setAttributedTitle_(_attr(look.CLOSE, color(t["muted"]), size=12.0))
        close.sizeToFit()
        close.setAccessibilityIdentifier_("shot:close")
        cx = BAR_W - PAD - 4 - close.frame().size.width
        close.setFrameOrigin_(NSMakePoint(cx, y + 6))
        esc.setFrameOrigin_(NSMakePoint(cx - esc.frame().size.width - 6, y + 7))
        card.addSubview_(esc)
        card.addSubview_(close)
        y += 44
        rows: list[Any] = [self._row([self._button(b) for b in view.buttons
                                      if b.key != "close"])]
        if view.notice is not None:
            rows.append(self._box(t["warn_bg"], t["warn_border"], inner, [
                _label(view.notice.text, color(t["warn_text"]), inner - 20),
                self._row([self._button(b) for b in view.notice.buttons])]))
        if view.banner:
            fill, border, ink = ((t["warn_bg"], t["warn_border"], t["warn_text"]) if view.warn
                                 else (t["highlight"], t["card_border"], t["text"]))
            rows.append(self._box(fill, border, inner,
                                  [_label(view.banner, color(ink), inner - 20)]))
        r = view.result
        if r is not None:
            head = _label(r.title + (f"  ·  {r.note}" if r.note else ""), color(t["muted"]),
                          inner, lines=1, size=12.0)
            rows.append(head)
            if r.text is not None:
                text = _label(r.text, color(t["text"]), inner - 24, size=13.5)
                th = min(240.0, text.frame().size.height)
                scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, inner - 4, th + 4))
                scroll.setHasVerticalScroller_(text.frame().size.height > 240)
                scroll.setDrawsBackground_(False)
                doc = _Flipped.alloc().initWithFrame_(
                    NSMakeRect(0, 0, inner - 24, text.frame().size.height))
                doc.addSubview_(text)
                scroll.setDocumentView_(doc)
                rows.append(self._box(t["card"], t["card_border"], inner, [scroll]))
                if r.actions:
                    rows.append(self._row([self._button(b) for b in r.actions]))
            elif r.state == "loading":
                rows.append(_label("…", color(t["muted"]), inner, size=12.0))
        for v in rows:
            v.setFrameOrigin_(NSMakePoint(PAD + 4, y))
            card.addSubview_(v)
            y += v.frame().size.height + 10
        card.setFrameSize_((BAR_W, y + PAD - 4))
        self._bar.setContentView_(card)
        self._bar.setContentSize_((BAR_W, y + PAD - 4))

    def hide_bar(self) -> None:
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

