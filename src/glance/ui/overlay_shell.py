"""ShellOverlay: the OverlayPort and GhostPort over Glance's GNOME Shell extension (Wayland).

The pieces are drawn by the extension from the scene built here (`glance.ui.scene`); this
class keeps the views and anchors, places every piece with the same functions as the X11
overlay (`glance.ui.placement`) and sends the result. Several changes in one main-loop turn
become one `Render` (`post` runs the render once the current work is done).

The extension moves the pieces along with the field's window in the same frame and hides
them while it is resized; glanced then re-places them from the fresh field rectangles capture
sends. Its own anchors are moved along too (`on_geometry`), so a render in between does not
put the pieces back where the window was.

While the extension is not on the bus nothing is drawn: every call is a no-op and the
overlay draws again as soon as it appears (`on_extension`).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from glance.log import log_event
from glance.protocol import Rect
from glance.shellbus import ShellBus, ShellUnavailable
from glance.ui import scene as sc
from glance.ui.chip_model import ChipView
from glance.ui.icon_toolbar import HOVER_OPENS, place_toolbar, toolbar_piece
from glance.ui.panel_model import PanelView, suggested_box
from glance.ui.placement import Anchor, clamp_to_monitor, place_status, place_strip
from glance.ui.status_model import StatusState, StatusView
from glance.ui.strip_model import StripView, scroll_max_h

ICON = 22
ICON_X, ICON_Y = 1, 5
ICON_BOX = (30, 28)
DRAWER_MIN_W = 520
SEL_W = 460
GAP = 4
STRIP_H = 46     # one line of strip until the extension reports its real height
PANEL_H = 220
RESHOW_MS = 400
GHOST_FONT_FACTOR = 0.81  # as ghost_gtk: the caret height times this is the font size
LINE_FACTOR = 1.2         # a line box is about this many times the font size  # a resized window: shown again by now even without a fresh snapshot

Post = Callable[[Callable[[], None]], None]
Later = Callable[[int, Callable[[], None]], Any]


class ShellOverlay:
    """OverlayPort (and, through `ghost`, GhostPort) drawn by the shell extension."""

    def __init__(self, bus: ShellBus, post: Post, call_later: Later,
                 tokens: Callable[[], dict[str, str]],
                 on_chip_click: Callable[[], None],
                 on_panel_action: Callable[[str, dict[str, Any]], None],
                 use_key: Callable[[], str] = lambda: "Alt+Return",
                 icon_toolbar: Callable[..., Any] = lambda on_purpose=False: None) -> None:
        self._bus = bus
        # the icon's toolbar now (None: not now); `on_purpose`: a right-click asks for it
        self._icon_toolbar = icon_toolbar
        self._menu: Any = None  # the IconToolbar on show
        self._post = post
        self._later = call_later
        self._tokens = tokens
        self._on_chip_click = on_chip_click
        self._act = on_panel_action
        self._use_key = use_key
        self._status: StatusView | None = None
        self._anchor: Anchor | None = None
        self._strip: StripView | None = None
        self._panel: PanelView | None = None
        self._panel_anchor: Anchor | None = None
        self._panel_point = (0, 0)
        self._ghost: tuple[str, int, int, int, int] | None = None
        self._selmark: tuple[Any, int, int] | None = None  # selection actions' mark / bar
        self._sizes = {"strip_h": STRIP_H, "panel_h": PANEL_H, "selmark_w": 0, "selmark_h": 0,
                       "toolbar_w": 0, "toolbar_h": 0}
        self._window: dict[str, Any] | None = None
        self._monitors: list[Rect] = []
        self._queued = False
        self._resized = 0  # renders since the last resize: a pending reshow is moot
        self._ask_done: Callable[[str, bool], None] | None = None
        self._placed: dict[str, Any] = {}  # what was last sent, for the test accessors
        self.ghost = ShellGhost(self)

    # ------------------------------------------------------------------ OverlayPort

    def show_chip(self, view: ChipView, x: int, y: int) -> None:
        """Kept for the state it reports; nothing is drawn for it (the status icon is)."""

    def hide_chip(self) -> None:
        pass

    def show_status(self, view: StatusView, anchor: Anchor) -> None:
        if view.state is StatusState.HIDDEN:
            self.hide_status()
            return
        self._status, self._anchor = view, anchor
        self._queue()

    def hide_status(self) -> None:
        self._menu = None
        if self._status is not None:
            self._status = None
            self._queue()

    def show_strip(self, view: StripView, anchor: Anchor) -> None:
        self._strip, self._anchor = view, anchor
        self._queue()

    def hide_strip(self) -> None:
        if self._strip is not None:
            self._strip = None
            self._queue()

    def show_panel(self, view: PanelView, x: int, y: int, anchor: Anchor | None = None) -> None:
        self._panel, self._panel_anchor, self._panel_point = view, anchor, (x, y)
        if anchor is not None:
            self._anchor = anchor
        self._queue()

    def hide_panel(self) -> None:
        if self._panel is not None:
            self._panel = None
            self._panel_anchor = None
            self._queue()

    def show_selmark(self, view: Any, x: int, y: int) -> None:
        self._selmark = (view, x, y)
        self._queue()

    def hide_selmark(self) -> None:
        if self._selmark is not None:
            self._selmark = None
            self._queue()

    def selmark_hit(self, x: int, y: int) -> bool:
        r = self._placed.get("selmark") if self._selmark is not None else None
        return r is not None and r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3]

    def selmark_geometry(self) -> tuple[int, int, int, int] | None:
        return self._placed.get("selmark") if self._selmark is not None else None

    def monitors(self) -> list[Rect]:
        if not self._monitors and self._bus.available:
            try:
                self._monitors = sc.rects(self._bus.call("Monitors"))
            except ShellUnavailable:
                pass
        return list(self._monitors)

    def copy_to_clipboard(self, text: str) -> None:
        try:
            self._bus.call("ClipboardSet", {"which": "clipboard", "text": text})
        except ShellUnavailable:
            log_event("shell_copy_failed")

    def ask_text(self, prompt: str, on_done: Callable[[str, bool], None],
                 at: tuple[int, int] | None = None) -> None:
        self._ask_done = on_done
        try:
            payload: dict[str, Any] = {"prompt": prompt, "placeholder": sc.ASK_PLACEHOLDER}
            if at is not None:  # the caller's place for it (the shot card's body)
                payload["at"] = [int(at[0]), int(at[1])]
            self._bus.call("Ask", payload)
        except ShellUnavailable:
            self._ask_done = None

    # ------------------------------------------------------------------ from the extension

    def on_extension(self, available: bool) -> None:
        """The extension appeared (draw everything again) or went away."""
        if available:
            self._monitors = []
            self._queue()

    def on_window(self, state: dict[str, Any]) -> None:
        """WindowChanged / GeometryChanged: the focused window and the monitors."""
        mons = sc.rects(state.get("monitors"))
        if mons:
            self._monitors = mons
        new = state.get("focus") if isinstance(state.get("focus"), dict) else None
        old = self._window
        self._window = new
        reason = state.get("reason")
        if new is None or old is None or str(new.get("id")) != str(old.get("id")):
            if reason == "monitors":
                self._queue()
            return
        try:
            ox, oy, ow, oh = old["frame"]
            nx, ny, nw, nh = new["frame"]
        except (KeyError, TypeError, ValueError):
            return
        dx, dy = nx - ox, ny - oy
        if dx or dy:
            # The extension moved the pieces already; keep the anchors with them so the next
            # render does not put them back where the window was.
            if self._anchor is not None:
                self._anchor = self._anchor.moved(dx, dy)
            if self._panel_anchor is not None:
                self._panel_anchor = self._panel_anchor.moved(dx, dy)
            if self._ghost is not None:
                text, x, y, px, mw = self._ghost
                self._ghost = (text, x + dx, y + dy, px, mw)
        if (nw, nh) != (ow, oh):
            # Hidden by the extension until placed again: capture's snapshot of the settled
            # window does that; if none comes, show them where they were, moved along.
            self._resized += 1
            mark = self._resized
            self._later(RESHOW_MS, lambda: self._reshow(mark))
        elif reason == "monitors":
            self._queue()

    def _reshow(self, mark: int) -> None:
        if mark == self._resized:
            self._queue()

    def on_clicked(self, payload: dict[str, Any]) -> None:
        target = payload.get("target")
        if target == "status":
            self._menu = None
            self._on_chip_click()
            self._queue()
            return
        if target == "hover":  # the pointer rested on the icon (older extensions send it)
            if not HOVER_OPENS:  # resting on the icon opens nothing: a right-click does
                return
            self._menu = self._icon_toolbar(False) if self._status is not None else None
            self._queue()
            return
        if target == "rightclick":  # the toolbar at once, typing or not; again: closed
            self._menu = None if self._menu is not None or self._status is None else \
                self._icon_toolbar(True)
            self._queue()
            return
        if target == "hover_end":  # the pointer left the icon and the toolbar
            if self._menu is not None:
                self._menu = None
                self._queue()
            return
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        action = args.get("action")
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        if target == "action" and isinstance(action, str):
            if self._menu is not None:
                self._menu = None
                self._queue()
            self._act(action, dict(inner))

    def on_sized(self, payload: dict[str, Any]) -> None:
        changed = False
        for key in ("strip_h", "panel_h", "selmark_w", "selmark_h", "toolbar_w", "toolbar_h"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and value > 0 and int(value) != self._sizes[key]:
                self._sizes[key] = int(value)
                changed = True
        if changed:
            self._queue()

    def on_ask_done(self, payload: dict[str, Any]) -> None:
        done, self._ask_done = self._ask_done, None
        text = payload.get("text")
        if done is not None and isinstance(text, str):
            done(text, bool(payload.get("focus_returned")))

    # ------------------------------------------------------------------ ghost

    def show_ghost(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None:
        self._ghost = (text, x, y, font_px, max_width)
        self._queue()

    def hide_ghost(self) -> None:
        if self._ghost is not None:
            self._ghost = None
            self._queue()

    # ------------------------------------------------------------------ rendering

    def _layout(self, force: bool = False) -> None:
        """Place everything again (the monitors changed)."""
        self._queue()

    def _queue(self) -> None:
        if not self._queued:
            self._queued = True
            self._post(self._render)

    def _render(self) -> None:
        self._queued = False
        self._resized += 1  # any render places the pieces again: a pending reshow is moot
        built = self.build()
        if not self._bus.available:
            return
        try:
            self._bus.call("Render", built)
        except ShellUnavailable:
            pass

    def build(self) -> dict[str, Any]:
        """The scene for the current views (also what the test accessors report)."""
        tokens = self._tokens()
        mons = self.monitors()
        status = strip = panel = ghost = None
        placed: dict[str, Any] = {}
        anchor = self._anchor
        if self._status is not None and anchor is not None:
            at = place_status(anchor, ICON, mons)
            if at is not None:
                x, y = clamp_to_monitor(at[0] - ICON_X, at[1] - ICON_Y, ICON_BOX, mons)
                status = sc.status_piece(self._status, x, y)
                placed["status"] = (x + ICON_X, y + ICON_Y, ICON, ICON)
                if self._menu is not None:
                    # under the caret line, its right end under the icon's (sized by the
                    # extension once drawn: a first guess until then)
                    size = (self._sizes["toolbar_w"] or 600, self._sizes["toolbar_h"] or 38)
                    icon = Rect(x + ICON_X, y + ICON_Y, ICON, ICON)
                    tx, ty = place_toolbar(icon, anchor.caret or anchor.prev_char, size, mons)
                    status["toolbar"] = {**toolbar_piece(self._menu), "x": tx, "y": ty}
                    placed["toolbar"] = (tx, ty, *size)
        field_drawer = self._panel is not None and self._panel_anchor is not None
        # While the field's panel is open it shows the corrected text itself ("Suggested"):
        # the strip is not drawn on its own then.
        shown_strip = None if field_drawer else self._strip
        if (shown_strip is not None or field_drawer) and anchor is not None:
            first = place_strip(anchor, 1, mons)
            if first is not None:
                mon = next((m for m in mons if m.x <= first.x < m.x + m.w),
                           mons[0] if mons else None)
                strip_h = self._sizes["strip_h"] if shown_strip is not None else 0
                card_w = first.w
                card_h = 0
                if field_drawer:
                    card_w = min(max(first.w, DRAWER_MIN_W), mon.w if mon else first.w)
                    card_h = self._sizes["panel_h"]
                gap = GAP if shown_strip is not None and field_drawer else 0
                p = place_strip(anchor, strip_h + gap + card_h, mons)
                if p is not None:
                    if p.above:
                        card_y, strip_y = p.y, p.y + card_h + gap
                    else:
                        strip_y, card_y = p.y, p.y + strip_h + gap
                    if shown_strip is not None:
                        strip = sc.strip_piece(shown_strip, p.x, strip_y, p.w, tokens,
                                               self._use_key(),
                                               scroll_max_h(mons, p.x, strip_y))
                        placed["strip"] = (p.x, strip_y, p.w, strip_h)
                    if field_drawer:
                        assert self._panel is not None
                        card_x = p.x
                        if mon is not None:
                            card_x = max(mon.x, min(card_x, mon.x + mon.w - card_w))
                        s = self._strip
                        box = suggested_box(self._panel, s)
                        suggested = None if box is None else \
                            sc.suggested_piece(box, tokens, self._use_key())
                        panel = sc.panel_piece(self._panel, card_x, card_y, card_w, True,
                                               suggested, s.note if s is not None else None)
                        placed["panel"] = (card_x, card_y, card_w, card_h)
        if self._panel is not None and self._panel_anchor is None:
            size = (SEL_W, self._sizes["panel_h"])
            if self._panel.mode == "result":
                # under the selected line, never over it (`selection_bar.place_result`)
                from glance.ui import selection_bar as sb

                x, y = sb.place_result(self._panel_point, size, mons)
            else:
                x, y = clamp_to_monitor(*self._panel_point, size, mons)
            panel = sc.panel_piece(self._panel, x, y, SEL_W, False)
            placed["panel"] = (x, y, SEL_W, self._sizes["panel_h"])
        if self._ghost is not None:
            text, x, y, font_px, max_width = self._ghost
            ghost = sc.ghost_piece(text, x, y, font_px, max_width)
        selmark = None
        if self._selmark is not None:
            from glance.ui import selection_bar as sb

            view, x, y = self._selmark
            size = sb.MARK_BOX if view.state == "mark" else (
                self._sizes["selmark_w"] or 640, self._sizes["selmark_h"] or sb.BAR_H)
            x, y = sb.place_bar((x, y), size, mons)
            selmark = sc.selmark_piece(view, x, y)
            placed["selmark"] = (x, y, *size)
        self._placed = placed
        window = self._window if self._window_matches(anchor) else None
        return sc.scene(tokens, window, status=status, strip=strip, panel=panel, ghost=ghost,
                        sizes=self._sizes, selmark=selmark)

    def _window_matches(self, anchor: Anchor | None) -> bool:
        """Follow the focused window only if the field is in it."""
        w = self._window
        return bool(w and anchor is not None and anchor.pid and w.get("pid") == anchor.pid)

    # ------------------------------------------------------------------ test access

    def status_geometry(self) -> tuple[int, int, int, int] | None:
        return self._placed.get("status") if self._status is not None else None

    def status_state(self) -> dict[str, Any] | None:
        v = self._status
        if v is None or "status" not in self._placed:
            return None
        return {"state": v.state.value, "badge": v.badge, "spinning": v.spin}

    def strip_geometry(self) -> tuple[int, int, int, int] | None:
        return self._placed.get("strip") if self._strip is not None else None

    def strip_text(self) -> str | None:
        v = self._strip
        if v is None or "strip" not in self._placed:
            return None
        return v.text or v.guard or v.note

    def panel_geometry(self) -> tuple[int, int, int, int] | None:
        return self._placed.get("panel") if self._panel is not None else None

    def toolbar_items(self) -> list[str] | None:
        """The icon's toolbar's item labels, None while it is closed."""
        m = self._menu
        return None if m is None or "toolbar" not in self._placed else \
            [a.label for a in m.actions]


class ShellGhost:
    """GhostPort: the ghost text is one more piece of the overlay's scene."""

    def __init__(self, overlay: ShellOverlay) -> None:
        self._overlay = overlay

    def show(self, text: str, x: int, y: int, font_px: int, max_width: int) -> None:
        # x is the caret's right edge, y its top; the line box is centred in the caret
        # rectangle as on X11 (its height is the font size over GHOST_FONT_FACTOR).
        font_px = max(1, font_px)
        caret_h = round(font_px / GHOST_FONT_FACTOR)
        top = y + (caret_h - round(font_px * LINE_FACTOR)) // 2
        self._overlay.show_ghost(text, x, top, font_px, max(1, max_width))

    def hide(self) -> None:
        self._overlay.hide_ghost()
