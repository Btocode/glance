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
    NSAttributedString,
    NSBezierPath,
    NSColor,
    NSCursor,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSPasteboard,
    NSScreenSaverWindowLevel,
    NSScrollView,
    NSTextField,
    NSZeroRect,
)
from Foundation import NSData, NSMakePoint, NSMakeRect, NSObject

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
    _chip,
    _fit,
    _Flipped,
    _font,
    _glyph,
    _keycap,
    _label,
    _mark_view,
    _mono,
    _Rows,
    _semibold,
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

TOOLBAR_H = 40.0  # the slim bar; the card grows under it
TOOLBAR_PAD = 4.0
ITEM_H = 28.0
ITEM_PAD = 8.0
CARD_MIN_W = 460.0  # the card, once it has a body
INSET = 8.0  # the body's margin in the card
BODY_PAD = 12.0
RESULT_MAX_H = 260.0  # longer text scrolls
FOOT_H = 30.0
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


# ---------------------------------------------------------------------- the toolbar's parts


class _Sep(_Flipped):
    """A toolbar's 1px divider (`ink`)."""

    def drawRect_(self, _rect):
        ink = getattr(self, "ink", None)
        if ink is not None:
            ink.setFill()
            NSBezierPath.fillRect_(self.bounds())


def _separator(t: dict[str, str]) -> Any:
    sep = _Sep.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 18))
    sep.ink = color(t["hairline"])
    return sep


def _item(target: Any, t: dict[str, str], b: bm.ShotButton, fn: Callable[[], None],
          label: bool = True, glyph: str | None = None) -> Any:
    """One toolbar item, as the selection bar draws them: a line glyph and its label; the
    selected one on the highlight in the accent, semibold; a muted one at half strength (a
    click says why)."""
    glyph = glyph or bm.GLYPHS.get(b.key)
    parts: list[Any] = []
    if glyph in look.GLYPHS:
        parts.append(_glyph(glyph, color(t["muted"] if b.muted else t["accent"]), 13.0))
    if label:
        fg = color(t["accent"] if b.selected else t["body"])
        font = _semibold(13.0) if b.selected else _font(size=13.0)
        lab = NSTextField.labelWithString_("")
        lab.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
            b.label, {NSFontAttributeName: font, NSForegroundColorAttributeName: fg}))
        lab.sizeToFit()
        parts.append(lab)
    width = 2 * ITEM_PAD + sum(p.frame().size.width for p in parts) + 5 * (len(parts) - 1)
    box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, ITEM_H))
    box.fill = color(t["highlight_rgba"]) if b.selected else None
    box.border, box.radius = None, 6.0
    x = ITEM_PAD
    for p in parts:
        f = p.frame()
        p.setFrameOrigin_(NSMakePoint(x, (ITEM_H - f.size.height) / 2))
        box.addSubview_(p)
        x += f.size.width + 5
    cover = _Button.alloc().initWithFrame_(NSMakeRect(0, 0, width, ITEM_H))
    cover.setTitle_("")
    cover.setBordered_(False)
    cover.setTransparent_(True)
    cover.setTarget_(target)
    cover.setAction_("fire:")
    cover.setTag_(target.add(fn))
    cover.setRefusesFirstResponder_(True)
    cover.setAccessibilityIdentifier_(f"shot:{b.key}")
    tip = b.tip or (None if label else b.label)
    if tip:
        cover.setToolTip_(tip)
    box.addSubview_(cover)
    if b.muted:
        box.setAlphaValue_(0.5)
    return box


class _ShotField(NSTextField):
    """Ask's question, typed in the card."""


class _AskDelegate(NSObject):
    """The field's Return and Escape (Escape would otherwise ask the field to complete)."""

    def control_textView_doCommandBySelector_(self, _control, _text_view, selector):
        owner = getattr(self, "owner", None)
        name = str(selector)
        if owner is None:
            return False
        if name in ("insertNewline:", "insertNewlineIgnoringFieldEditor:"):
            owner.ask_key("submit")
            return True
        if name in ("cancelOperation:", "complete:"):
            owner.ask_key("cancel")
            return True
        return False


# ---------------------------------------------------------------------- MacShot


class MacShot:
    """ShotUiPort on macOS. `post(fn)` runs `fn` on the main thread (capture callbacks
    arrive on others)."""

    def __init__(self, on_action: Callable[[str, dict[str, Any]], None],
                 post: Callable[[Callable[[], None]], None],
                 notify: Callable[[str, str], None] = lambda _s, _b: None,
                 keys: Callable[[bool], None] = lambda _taken: None) -> None:
        """`on_action(key, args)`: the controller's `action`. `keys(True)` when one of
        Glance Shot's panels takes the keyboard (Ask's field, Mark), `keys(False)` when it
        gives it back: Escape must reach that panel, not close the shot."""
        self._act = on_action
        self._keys = keys
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
        self._bar_wh = (0, 0)
        self._placed: tuple[Rect, tuple[int, int]] | None = None  # (region, top-left)
        self._view_shown: tuple[bm.ShotBarView, Rect] | None = None
        self._field: Any = None  # Ask's question, while it is typed
        self._ask_delegate = _AskDelegate.alloc().init()
        self._keys_taken = False
        self._marker: Marker | None = None
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

    def _plain_button(self, b: bm.ShotButton, fn: Callable[[], None] | None = None) -> Any:
        btn = _Button.buttonWithTitle_target_action_(b.label, self._target, "fire:")
        btn.setTag_(self._target.add(fn or (lambda: self._act(b.key, {}))))
        btn.setRefusesFirstResponder_(True)
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        if b.tip:
            btn.setToolTip_(b.tip)
        btn.setAccessibilityIdentifier_(f"shot:{b.key}")
        return btn

    def _button(self, b: bm.ShotButton, fn: Callable[[], None] | None = None) -> Any:
        """A result action or a notice button: the primary one an emerald pill, the others
        outlined."""
        t = self.t
        btn = self._plain_button(b, fn)
        primary = b.primary and not b.muted
        fg = t["on_accent"] if primary else t["muted"] if b.muted else t["text"]
        btn.setAttributedTitle_(_attr(b.label, color(fg), bold=primary, size=12.5))
        btn.sizeToFit()
        f = btn.frame()
        btn.setFrameSize_((f.size.width + 24, 28.0))
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

    def _toolbar(self, view: bm.ShotBarView, card: Any) -> float:
        """The slim bar, as the selection bar draws it: the mark, the tabs │ Mark · Copy ·
        Save │ ✕. Laid into `card` at its top; gives back its width. It never moves while
        the card grows under it."""
        t = self.t
        lead = _mark_view(16.0)
        lead.setToolTip_(f"{bm.TITLE} · {view.size}")
        parts: list[tuple[Any, float]] = [(lead, 6.0)]
        for b in view.buttons:
            if b.key != "close":
                parts.append((_item(self._target, t, b, lambda k=b.key: self._act(k, {})), 1.0))
        parts.append((_separator(t), 1.0))
        for b in view.tools:
            parts.append((_item(self._target, t, b, lambda k=b.key: self._act(k, {})), 1.0))
        parts.append((_separator(t), 1.0))
        close = bm.ShotButton("close", f"{look.CLOSE} (Esc)")
        parts.append((_item(self._target, t, close, lambda: self._act("close", {}),
                            label=False), 0.0))
        x = TOOLBAR_PAD + 4
        for v, gap in parts:
            f = v.frame()
            if v.isKindOfClass_(_Sep):
                x += 4
                v.setFrameOrigin_(NSMakePoint(x, (TOOLBAR_H - f.size.height) / 2))
                x += 1 + 5
            else:
                v.setFrameOrigin_(NSMakePoint(x, (TOOLBAR_H - f.size.height) / 2))
                x += f.size.width + gap
            card.addSubview_(v)
        return x + TOOLBAR_PAD

    def _status(self, view: bm.ShotBarView, width: float, right: bool = False) -> Any:
        t = self.t
        ink = t["warn_text"] if view.warn else t["accent"]
        status = _label(view.banner or "", color(ink), max(40.0, width), lines=1, size=12.5)
        if right:
            status.setAlignment_(2)
        status.setToolTip_(view.banner)
        status.setAccessibilityIdentifier_("shot:status")
        return status

    def _body(self, view: bm.ShotBarView, width: float) -> Any:
        """The notice, the question's entry, or the result (its choices, text, skeleton or
        why it failed)."""
        t = self.t
        inner = width - 2 * BODY_PAD
        fill, border = t["card"], t["card_border"]
        if view.notice is not None:
            fill, border = t["warn_bg"], t["warn_border"]
        box = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, width, 40))
        box.fill, box.border, box.radius = color(fill), color(border), 8.0
        box.setAccessibilityIdentifier_("shot:body")
        rows = _Rows(box, inner, top=BODY_PAD, left=BODY_PAD)
        if view.notice is not None:
            rows.full(_label(view.notice.text, color(t["warn_text"]), inner, size=13.0), gap=0)
        elif view.asking:
            self._ask_body(rows, inner, view)
        elif view.result is not None:
            self._result(rows, view.result, inner)
        box.setFrameSize_((width, max(36.0, rows.y + BODY_PAD - 6)))
        return box

    def _ask_body(self, rows: Any, inner: float, view: bm.ShotBarView) -> None:
        t = self.t
        rows.full(_label(view.ask_prompt, color(t["muted"]), inner, bold=True, lines=1,
                         size=12.0), gap=8)
        field = _ShotField.alloc().initWithFrame_(NSMakeRect(0, 0, inner, 28))
        field.setPlaceholderString_(view.ask_placeholder)
        field.setFont_(_font(size=13.0))
        field.setBezelStyle_(1)  # rounded
        field.setFocusRingType_(1)  # none: the bezel says where it is
        field.setAccessibilityIdentifier_("shot:ask")
        self._ask_delegate.owner = self
        field.setDelegate_(self._ask_delegate)
        rows.full(field, gap=8)
        hint: list[Any] = []
        for key, what in ((look.RETURN_KEY, view.ask_go), (look.ESC_KEY, "back")):
            hint.append(_keycap(key, t))
            lab = _label(what, color(t["muted"]), 60, lines=1, size=12.0)
            lab.sizeToFit()
            hint.append(lab)
        rows.run(hint, gap=6)
        self._field = field

    def _result(self, rows: Any, r: bm.ShotResult, inner: float) -> None:
        t = self.t
        title = _label(r.title, color(t["muted"]), inner, bold=True, lines=1, size=12.0)
        title.sizeToFit()
        right = []
        if r.note and r.state != "failed":
            room = inner - title.frame().size.width - 12
            note = _label(r.note, color(t["muted"]), room, lines=1, size=12.0)
            note.sizeToFit()
            if note.frame().size.width > room:
                note.setFrameSize_((room, note.frame().size.height))
            right.append(note)
        rows.run([title], gap=8, right=right)
        if r.state == "choose":
            rows.run([_chip(self._target, t, c.label, c.selected,
                            lambda k=c.key: self._act(k, {}), f"shot:{c.key}", c.tip)
                      for c in r.choices], gap=6)
            return
        if r.state == "failed":
            rows.full(_label(r.note or "That did not work. Try again", color(t["warn_text"]),
                             inner, size=12.5), gap=6)
            return
        if r.text is not None:
            width = inner - 14
            if r.title == "Text":  # what was read, line for line
                text = _label(r.text, color(t["text"]), width, size=12.5)
                text.setFont_(_mono(12.5))
                _fit(text, width)
            else:
                text = _label(r.text, color(t["text"]), width, size=13.5)
            full = text.frame().size.height
            th = min(RESULT_MAX_H, full)
            scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, inner, th))
            scroll.setHasVerticalScroller_(full > RESULT_MAX_H)
            scroll.setAutohidesScrollers_(True)
            scroll.setDrawsBackground_(False)
            scroll.setBorderType_(0)  # NSNoBorder
            doc = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, width, full))
            doc.addSubview_(text)
            scroll.setDocumentView_(doc)
            rows.full(scroll, gap=6)
        elif r.state == "loading":
            for share in look.SKELETON_SUGGESTED:  # skeleton lines, the answer's shape
                rows.full(_skeleton(t, inner * share), gap=look.SKELETON_GAP + 4)

    def _footer(self, view: bm.ShotBarView, width: float) -> Any | None:
        """The notice's buttons or the result's actions on the left; the status on the
        right. None when there is neither."""
        actions: tuple[bm.ShotButton, ...] = ()
        if view.notice is not None:
            actions = view.notice.buttons
        elif view.result is not None and view.result.text is not None and not view.asking:
            actions = view.result.actions
        if not actions and not view.banner:
            return None
        row = _Flipped.alloc().initWithFrame_(NSMakeRect(0, 0, width, FOOT_H))
        rows = _Rows(row, width, top=0, left=0)
        buttons = [self._button(b) for b in actions]
        right = []
        if view.banner:
            used = sum(b.frame().size.width + 6 for b in buttons)
            room = max(80.0, width - used - 8) if used < width else width
            right.append(self._status(view, room, right=True))
        if buttons and right and sum(b.frame().size.width + 6 for b in buttons) \
                + right[0].frame().size.width > width:
            rows.run(buttons)  # no room beside them: the status goes under
            rows.run([], right=right)
        else:
            rows.run(buttons, right=right)
        row.setFrameSize_((width, max(FOOT_H, rows.y - 6)))
        # centre a lone status line on the buttons' row
        return row

    def show_bar(self, view: bm.ShotBarView, region: Rect) -> None:
        self._view_shown = (view, region)
        if self._marker is not None:  # marking: the card comes back when it is done
            return
        self._region = region
        self.t = t = tokens()
        key = (view, region, t["surface"])
        if key != self._bar_key:
            self._bar_key = key
            self._release_keys()
            self._target.reset()
            self._bar_wh = self._build(view)
        w, h = self._bar_wh
        x, y = self._position(region, (w, h))
        # One frame change, content already in place: the card does not flicker, and its
        # top-left (the toolbar) stays put for the whole shot.
        place(self._bar, Rect(x, y, w, h))
        self._bar.invalidateShadow()
        self._show_frame(region)
        if not self._bar.isVisible():
            self._bar.orderFrontRegardless()
        if view.asking and self._field is not None and not self._keys_taken:
            self._take_keys()

    def _position(self, region: Rect, size: tuple[int, int]) -> tuple[int, int]:
        """Placed once per shot (beside the region, as the selection bar); after that the
        top-left stays where it is while the card grows and shrinks under it, moved up only
        if it would leave the screen."""
        w, h = size
        mons = monitors()
        if self._placed is not None and self._placed[0] == region:
            x, y = self._placed[1]
            mon = geo.monitor_at(x, y, mons)
            if mon is not None and y + h > mon.y + mon.h:
                y = max(mon.y, mon.y + mon.h - h)
        else:
            x, y = geo.bar_position(region, (w, h), mons)
        self._placed = (region, (x, y))
        return x, y

    def _build(self, view: bm.ShotBarView) -> tuple[int, int]:
        t = self.t
        self._field = None
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, 400, TOOLBAR_H))
        card.fill, card.border, card.radius = color(t["surface"]), color(t["hairline"]), 10.0
        bar_w = self._toolbar(view, card)
        width = max(bar_w, CARD_MIN_W) if view.expanded else bar_w
        y = TOOLBAR_H
        if view.expanded:
            inner = width - 2 * INSET
            body = self._body(view, inner)
            body.setFrameOrigin_(NSMakePoint(INSET, y))
            card.addSubview_(body)
            y += body.frame().size.height + 8
            foot = self._footer(view, inner)
            if foot is not None:
                foot.setFrameOrigin_(NSMakePoint(INSET, y))
                card.addSubview_(foot)
                y += foot.frame().size.height + 8
            y += 2
        elif view.banner:
            status = self._status(view, width - 2 * INSET - 4)
            status.setFrameOrigin_(NSMakePoint(INSET + 4, y - 4))
            card.addSubview_(status)
            y += status.frame().size.height + 4
        card.setFrameSize_((width, y))
        self._bar.setContentView_(card)
        return round(width), round(y)

    def ask_point(self) -> tuple[int, int] | None:
        """Where a one-line entry of its own goes ("Other…" language): under the toolbar."""
        if not self._bar.isVisible():
            return None
        x, y, _w, _h = frame_of(self._bar)
        return x + 8, y + round(TOOLBAR_H) + 6

    # ------------------------------------------------------------------ Ask, in the card

    inline_ask = True  # the controller has Ask's question typed in the card, not a window

    def _take_keys(self) -> None:
        """The question's field gets the keyboard: the card becomes key, as a
        non-activating panel can (the app in front stays in front); Escape is no longer
        Glance's hotkey meanwhile, so it reaches the field."""
        field = self._field
        if field is None:
            return
        self._bar.allow_key = True
        self._keys_taken = True
        self._keys(True)
        self._bar.makeKeyWindow()
        self._bar.makeFirstResponder_(field)

    def _release_keys(self) -> None:
        if not self._keys_taken:
            return
        self._keys_taken = False
        self._bar.allow_key = False
        self._keys(False)
        if self._bar.isKeyWindow():  # the keyboard goes back to the app in front
            self._bar.orderOut_(None)

    def ask_key(self, what: str) -> None:
        """The field's Return ("submit") or Escape ("cancel")."""
        field = self._field
        text = str(field.stringValue() or "") if field is not None else ""
        self._release_keys()
        if what == "submit":
            self._act("ask_submit", {"text": text})
        else:
            self._act("ask_cancel", {})

    # ------------------------------------------------------------------ Mark

    def annotate(self, shot: Shot, done: Callable[[bytes | None], None]) -> None:
        """Mark up the shot (`Marker`); the card steps aside meanwhile and comes back."""
        if self._marker is not None:
            self._marker.cancel()
        self._release_keys()
        self._bar.orderOut_(None)
        self._hide_frame()

        def finished(png: bytes | None) -> None:
            self._marker = None
            done(png)
            if self._marker is None and self._view_shown is not None \
                    and self._region is not None and not self._bar.isVisible():
                self.show_bar(*self._view_shown)  # (the controller usually did already)

        self._marker = Marker(self, shot, finished)

    @property
    def marker(self) -> Any:
        return self._marker

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
        self._release_keys()
        self._region = None
        self._view_shown = None  # (first: a Mark cancelled here does not bring it back)
        if self._marker is not None:
            self._marker.cancel()
        self._hide_frame()
        self._bar.orderOut_(None)
        self._bar_key = None
        self._placed = None
        self._field = None

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


# ---------------------------------------------------------------------- Mark

MARK_TOOLS = (("pen", "Pen", "pen"), ("box", "Box", "box"), ("arrow", "Arrow", "arrow"),
              ("highlight", "Highlight", "lines"))
MARK_INK = (0.937, 0.267, 0.267, 1.0)        # red: pen, box, arrow
MARK_HIGHLIGHT = (0.98, 0.80, 0.08, 0.38)    # a highlighter's yellow, see-through
MARK_WIDTH, HIGHLIGHT_WIDTH = 3.0, 16.0
ARROW_HEAD, ARROW_SPREAD = 14.0, 0.45

Strokes = list[tuple[str, list[tuple[float, float]]]]


def draw_marks(ctx: Any, strokes: Strokes, scale: float = 1.0) -> None:
    """The marks on a CGContext whose y runs down, in the region's logical coordinates
    times `scale` (the PNG's pixels). The same colours and widths as the GTK renderer's."""
    import math

    import Quartz as Q

    Q.CGContextSetLineJoin(ctx, Q.kCGLineJoinRound)
    for tool, pts in strokes:
        if not pts:
            continue
        p = [(x * scale, y * scale) for x, y in pts]
        if tool == "highlight":
            Q.CGContextSetRGBStrokeColor(ctx, *MARK_HIGHLIGHT)
            Q.CGContextSetLineWidth(ctx, HIGHLIGHT_WIDTH * scale)
            Q.CGContextSetLineCap(ctx, Q.kCGLineCapButt)
        else:
            Q.CGContextSetRGBStrokeColor(ctx, *MARK_INK)
            Q.CGContextSetLineWidth(ctx, MARK_WIDTH * scale)
            Q.CGContextSetLineCap(ctx, Q.kCGLineCapRound)
        (x0, y0), (x1, y1) = p[0], p[-1]
        if tool == "box":
            Q.CGContextStrokeRect(ctx, Q.CGRectMake(min(x0, x1), min(y0, y1), abs(x1 - x0),
                                                    abs(y1 - y0)))
        elif tool == "arrow":
            Q.CGContextBeginPath(ctx)
            Q.CGContextMoveToPoint(ctx, x0, y0)
            Q.CGContextAddLineToPoint(ctx, x1, y1)
            angle = math.atan2(y1 - y0, x1 - x0)
            head = ARROW_HEAD * scale
            for side in (-ARROW_SPREAD, ARROW_SPREAD):
                Q.CGContextMoveToPoint(ctx, x1, y1)
                Q.CGContextAddLineToPoint(ctx, x1 - head * math.cos(angle + side),
                                          y1 - head * math.sin(angle + side))
            Q.CGContextStrokePath(ctx)
        else:
            Q.CGContextBeginPath(ctx)
            Q.CGContextMoveToPoint(ctx, *p[0])
            for q in p[1:]:
                Q.CGContextAddLineToPoint(ctx, *q)
            if len(p) == 1:
                Q.CGContextAddLineToPoint(ctx, p[0][0] + 0.1, p[0][1])
            Q.CGContextStrokePath(ctx)


def marked_png(shot: Shot, strokes: Strokes) -> bytes | None:
    """The shot's PNG with the marks drawn on it, at its own pixel size."""
    import Quartz as Q

    src = Q.CGImageSourceCreateWithData(NSData.dataWithBytes_length_(shot.png, len(shot.png)),
                                        None)
    img = Q.CGImageSourceCreateImageAtIndex(src, 0, None) if src is not None else None
    if img is None:
        return None
    w, h = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
    space = Q.CGColorSpaceCreateWithName(Q.kCGColorSpaceSRGB)
    ctx = Q.CGBitmapContextCreate(None, w, h, 8, 0, space, Q.kCGImageAlphaPremultipliedLast)
    Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, w, h), img)
    Q.CGContextTranslateCTM(ctx, 0, h)
    Q.CGContextScaleCTM(ctx, 1, -1)
    draw_marks(ctx, strokes, w / max(1, shot.rect.w))
    out = Q.CGBitmapContextCreateImage(ctx)
    return png_of(out) if out is not None else None


class _MarkCanvas(_Flipped):
    """The frozen region with the marks on it; hears the pointer and the keys (`marker`)."""

    def acceptsFirstResponder(self):
        return True

    def acceptsFirstMouse_(self, _event):
        return True

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), NSCursor.crosshairCursor())

    @objc.python_method
    def _at(self, event: Any) -> tuple[float, float]:
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        return float(p.x), float(p.y)

    def mouseDown_(self, event):
        self.marker.press(*self._at(event))

    def mouseDragged_(self, event):
        self.marker.drag(*self._at(event))

    def mouseUp_(self, event):
        self.marker.release(*self._at(event))

    def keyDown_(self, event):
        code = int(event.keyCode())
        if code == 53:
            self.marker.cancel()
        elif code in (36, 76):
            self.marker.finish()
        elif code == 6 and event.modifierFlags() & (1 << 20):  # ⌘Z
            self.marker.act("mark_undo")

    def performKeyEquivalent_(self, event):
        if int(event.keyCode()) == 6 and event.modifierFlags() & (1 << 20):
            self.marker.act("mark_undo")
            return True
        return False

    def drawRect_(self, _rect):
        m = getattr(self, "marker", None)
        if m is None:
            return
        b = self.bounds()
        if m.image is not None:
            m.image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                b, NSZeroRect, 2, 1.0, True, None)  # source-over
        ctx = NSGraphicsContext.currentContext()
        cg = ctx.CGContext() if hasattr(ctx, "CGContext") else ctx.graphicsPort()
        draw_marks(cg, m.strokes)
        color(m.owner.t["accent_fill"]).setStroke()
        ring = NSBezierPath.bezierPathWithRect_(
            NSMakeRect(1, 1, b.size.width - 2, b.size.height - 2))
        ring.setLineWidth_(2.0)
        ring.stroke()


class Marker:
    """Draw on a shot: the frozen region in a borderless, non-activating panel right where
    it was taken (key while it is up: the app in front stays in front), and a small bar of
    tools above it. Pen, Box, Arrow, Highlight; Undo (⌘Z); Done (Return) gives the marked-up
    PNG, Cancel (Esc) None. Escape is not Glance's hotkey meanwhile."""

    def __init__(self, owner: MacShot, shot: Shot, done: Callable[[bytes | None], None]) -> None:
        self.owner, self._shot, self._done = owner, shot, done
        self.tool = "pen"
        self.strokes: Strokes = []
        self._drawing = False
        self._finished = False
        self._target = Target.alloc().init()
        self.image = NSImage.alloc().initWithData_(
            NSData.dataWithBytes_length_(shot.png, len(shot.png)))
        r = shot.rect
        self.panel = make_panel(can_key=True)
        self.panel.setHasShadow_(False)
        self.canvas = _MarkCanvas.alloc().initWithFrame_(NSMakeRect(0, 0, r.w, r.h))
        self.canvas.marker = self
        self.panel.setContentView_(self.canvas)
        self.panel.on_cancel = self.cancel
        place(self.panel, r)
        self.bar = make_panel()
        self._build_bar()
        owner._keys(True)
        self.panel.orderFrontRegardless()
        self.bar.orderFrontRegardless()
        self.panel.makeKeyWindow()
        self.panel.makeFirstResponder_(self.canvas)
        log_event("shot_mark_open")

    # -- the bar of tools
    def _build_bar(self) -> None:
        t = self.owner.t
        self._target.reset()
        card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, 400, TOOLBAR_H))
        card.fill, card.border, card.radius = color(t["surface"]), color(t["hairline"]), 10.0
        parts: list[Any] = [_mark_view(16.0)]
        for key, label, glyph in MARK_TOOLS:
            b = bm.ShotButton(f"mark_{key}", label, selected=key == self.tool)
            parts.append(_item(self._target, t, b, lambda k=b.key: self.act(k), glyph=glyph))
        parts.append(_separator(t))
        for key, label in (("mark_undo", "Undo"), ("mark_cancel", "Cancel")):
            parts.append(_item(self._target, t, bm.ShotButton(key, label),
                               lambda k=key: self.act(k)))
        done = self.owner._plain_button(bm.ShotButton("mark_done", "Done", primary=True))
        done.setTarget_(self._target)
        done.setTag_(self._target.add(self.finish))
        done.setAttributedTitle_(_attr("Done", color(t["on_accent"]), bold=True, size=13.0))
        done.sizeToFit()
        done.setFrameSize_((done.frame().size.width + 28, ITEM_H))
        done.layer().setCornerRadius_(7.0)
        done.layer().setBackgroundColor_(cgcolor(t["accent_fill"]))
        parts.append(done)
        x = TOOLBAR_PAD + 4
        for v in parts:
            f = v.frame()
            if v.isKindOfClass_(_Sep):
                x += 4
            v.setFrameOrigin_(NSMakePoint(x, (TOOLBAR_H - f.size.height) / 2))
            card.addSubview_(v)
            x += f.size.width + (6 if v is parts[0] else 5 if v.isKindOfClass_(_Sep) else 1)
        w = x + TOOLBAR_PAD + 2
        card.setFrameSize_((w, TOOLBAR_H))
        self.bar.setContentView_(card)
        self._place_bar(round(w), round(TOOLBAR_H))

    def _place_bar(self, w: int, h: int) -> None:
        r = self._shot.rect
        mons = monitors()
        mon = geo.monitor_at(r.x + r.w // 2, r.y + r.h // 2, mons) or Rect(0, 0, 10 ** 5, 10 ** 5)
        y = r.y - h - 8 if r.y - h - 8 >= mon.y else r.y + r.h + 8
        if y + h > mon.y + mon.h:
            y = r.y + 8  # no room around it: inside the region's top
        x = max(mon.x, min(r.x, mon.x + mon.w - w))
        place(self.bar, Rect(x, y, w, h))
        self.bar.invalidateShadow()

    def act(self, key: str) -> None:
        if key.startswith("mark_") and key[5:] in {k for k, _l, _g in MARK_TOOLS}:
            self.tool = key[5:]
            self._build_bar()
        elif key == "mark_undo":
            if self.strokes:
                self.strokes.pop()
                self.canvas.setNeedsDisplay_(True)
        elif key == "mark_cancel":
            self.cancel()
        elif key == "mark_done":
            self.finish()

    # -- drawing (view coordinates: the region's, top-left)
    def press(self, x: float, y: float) -> None:
        self._drawing = True
        self.strokes.append((self.tool, [(x, y)]))
        self.canvas.setNeedsDisplay_(True)

    def drag(self, x: float, y: float) -> None:
        if self._drawing and self.strokes:
            tool, pts = self.strokes[-1]
            if tool in ("box", "arrow"):
                pts[1:] = [(x, y)]
            else:
                pts.append((x, y))
            self.canvas.setNeedsDisplay_(True)

    def release(self, x: float, y: float) -> None:
        if self._drawing:
            self.drag(x, y)
            self._drawing = False

    # -- the end
    def finish(self) -> None:
        png = marked_png(self._shot, self.strokes) if self.strokes else None
        self._end(png)

    def cancel(self) -> None:
        self._end(None)

    def _end(self, png: bytes | None) -> None:
        if self._finished:
            return
        self._finished = True
        self.panel.orderOut_(None)
        self.bar.orderOut_(None)
        self.owner._keys(False)
        log_event("shot_mark_closed", marked=png is not None)
        self._done(png)
