"""Glance's look: one set of colour tokens, light and dark, and the GTK CSS built from them.

The pure part (tokens, contrast, CSS text) needs no display. `install()` puts the CSS on the
default screen and swaps it whenever the desktop switches between light and dark (GTK's
prefer-dark setting, its theme name, or GNOME's `color-scheme`).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

FONT = "Inter, Cantarell, 'Noto Sans', 'DejaVu Sans', sans-serif"

LIGHT: dict[str, str] = {
    "surface": "#ffffff",       # the drawer, the dialogs
    "strip": "#edf6f3",         # the collapsed suggestion
    "strip_border": "#d3e6df",
    "text": "#17191c",
    "body": "#2a2f2c",
    "muted": "#5a625e",
    "accent": "#0e6b5c",
    "accent_hover": "#0b5a4d",
    "on_accent": "#ffffff",
    "hairline": "#dfe3de",
    "card": "#f6f7f5",
    "card_border": "#e3e6e2",
    "hover": "#e4efeb",
    "warn_bg": "#fbf1dc",
    "warn_border": "#efd9a6",
    "warn_text": "#6b4400",
    "shadow": "rgba(23, 25, 28, 0.14)",
    "dot_quiet": "#8a918e",
    "dot_warn": "#c98a12",
    "dot_info": "#0e6b5c",
    "ghost": "#7d8783",
}

DARK: dict[str, str] = {
    "surface": "#1e2221",
    "strip": "#17302a",
    "strip_border": "#25463e",
    "text": "#eef1ef",
    "body": "#dfe6e2",
    "muted": "#a5afab",
    "accent": "#5cc4ae",
    "accent_hover": "#78d3c0",
    "on_accent": "#0b201b",
    "hairline": "#343a38",
    "card": "#262b29",
    "card_border": "#363d3a",
    "hover": "#21403a",
    "warn_bg": "#3a2e17",
    "warn_border": "#5a4720",
    "warn_text": "#f2cf86",
    "shadow": "rgba(0, 0, 0, 0.45)",
    "dot_quiet": "#8f9894",
    "dot_warn": "#e0a43a",
    "dot_info": "#5cc4ae",
    "ghost": "#8d9894",
}

# (foreground, background) token pairs that carry text: each must reach 4.5:1.
TEXT_PAIRS = (
    ("body", "strip"), ("accent", "strip"), ("muted", "strip"), ("on_accent", "accent"),
    ("text", "surface"), ("muted", "surface"), ("text", "card"), ("muted", "card"),
    ("warn_text", "warn_bg"), ("accent", "surface"), ("on_accent", "accent_hover"),
)


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb(tokens: dict[str, str], name: str) -> tuple[float, float, float]:
    """A token as cairo wants it (0..1 floats)."""
    return _rgb(tokens[name])


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two #rrggbb colours."""
    def lum(c: str) -> float:
        def chan(v: float) -> float:
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (chan(v) for v in _rgb(c))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css(t: dict[str, str]) -> str:
    """The stylesheet for every Glance window, in the colours `t`."""
    return f"""
.glance-root, .glance-root label, .glance-root button, .glance-root entry {{
    font-family: {FONT}; font-size: 14px; }}
.glance-root label {{ color: {t['body']}; }}
.glance-root button {{
    background-image: none; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none;
    min-height: 0; min-width: 0; }}
.glance-root button:focus {{ outline: 2px solid {t['accent']}; outline-offset: 1px; }}

/* the collapsed strip */
.glance-strip {{
    background-color: {t['strip']}; border: 1px solid {t['strip_border']};
    border-radius: 9px; padding: 8px 10px; }}
.glance-strip .glance-sentence {{ color: {t['body']}; font-size: 14px; }}
.glance-strip .glance-note {{ color: {t['muted']}; font-size: 13px; }}
.glance-strip .glance-guard {{ color: {t['warn_text']}; font-size: 13px; font-weight: 600; }}
.glance-strip.glance-warn {{ background-color: {t['warn_bg']};
                             border-color: {t['warn_border']}; }}
.glance-primary {{
    background-color: {t['accent']}; color: {t['on_accent']}; border: none;
    border-radius: 7px; padding: 6px 10px; font-size: 12px; font-weight: 600; }}
.glance-primary label {{ color: {t['on_accent']}; font-size: 12px; font-weight: 600; }}
.glance-primary:hover {{ background-color: {t['accent_hover']}; }}
.glance-quiet {{
    background-color: transparent; border: none; border-radius: 7px; padding: 4px 8px; }}
.glance-quiet label {{ color: {t['body']}; }}
.glance-quiet:hover, .glance-quiet.glance-on {{ background-color: {t['hover']}; }}

/* the drawer, select-anything's panel, the "Rewrite as…" entry */
.glance-drawer {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 12px; padding: 14px 16px;
    box-shadow: 0 6px 18px {t['shadow']}; }}
.glance-title {{ color: {t['text']}; font-weight: 600; }}
.glance-root label.glance-meta, .glance-meta label {{ color: {t['muted']}; font-size: 12px; }}
.glance-card {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 9px; padding: 9px 10px; }}
.glance-card:hover {{ border-color: {t['accent']}; }}
.glance-card .glance-card-title {{ color: {t['text']}; font-size: 13px; font-weight: 600; }}
.glance-card .glance-card-preview {{ color: {t['muted']}; font-size: 12px; }}
.glance-card .glance-kbd {{
    color: {t['muted']}; font-size: 11px; border: 1px solid {t['card_border']};
    border-radius: 4px; padding: 0 4px; background-color: {t['surface']}; }}
.glance-input {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 8px; padding: 7px 10px; }}
.glance-input label {{ color: {t['muted']}; font-size: 13px; }}
.glance-input:hover {{ border-color: {t['accent']}; }}
.glance-root entry {{
    background-color: {t['surface']}; color: {t['text']}; border: 1px solid {t['accent']};
    border-radius: 8px; padding: 7px 10px; font-size: 13px; box-shadow: none; }}
.glance-link {{
    background-color: transparent; border: none; border-radius: 6px; padding: 1px 4px; }}
.glance-link label {{ color: {t['muted']}; font-size: 12px; }}
.glance-link:hover label {{ color: {t['accent']}; }}
.glance-link.glance-strong label {{ color: {t['accent']}; font-weight: 600; }}
.glance-secondary {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 7px; padding: 5px 10px; }}
.glance-secondary label {{ color: {t['text']}; font-size: 12px; }}
.glance-secondary:hover {{ border-color: {t['accent']}; }}
.glance-banner {{
    background-color: {t['warn_bg']}; border: 1px solid {t['warn_border']};
    border-radius: 9px; padding: 8px 10px; }}
.glance-banner label {{ color: {t['warn_text']}; font-size: 13px; }}
.glance-banner .glance-link label {{ color: {t['warn_text']}; font-weight: 600; }}
.glance-preview {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 9px; padding: 8px 10px; }}
.glance-row {{ border-top: 1px solid {t['hairline']}; padding-top: 8px; }}
.glance-excerpt {{ color: {t['text']}; }}

/* the "Connect an AI" window */
.glance-window {{ background-color: {t['surface']}; }}
.glance-window .glance-headline {{ color: {t['text']}; font-size: 18px; font-weight: 600; }}
.glance-window frame, .glance-window frame > border {{ border: none; }}
.glance-window .glance-card {{ padding: 12px 14px; }}
.glance-window entry, .glance-window combobox button {{ border-radius: 8px; }}
"""


def gtk_prefers_dark(settings: Any) -> bool:
    if settings is None:
        return False
    if settings.get_property("gtk-application-prefer-dark-theme"):
        return True
    name = settings.get_property("gtk-theme-name") or ""
    return name.lower().endswith("-dark") or ":dark" in name.lower()


class Theme:
    """The installed stylesheet. `dark` is the current choice; `tokens` its colours.

    Listeners (`on_change`) are told after a switch, so windows that draw with cairo can
    redraw in the new colours.
    """

    def __init__(self) -> None:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

        self._gtk, self._gdk = Gtk, Gdk
        self._provider = Gtk.CssProvider()
        self._listeners: list[Callable[[], None]] = []
        self._gnome: Any = None
        self._forced: bool | None = None
        self.dark = False
        settings = Gtk.Settings.get_default()
        if settings is not None:
            for prop in ("gtk-application-prefer-dark-theme", "gtk-theme-name"):
                settings.connect(f"notify::{prop}", lambda *_a: self._update())
        try:
            from gi.repository import Gio

            source = Gio.SettingsSchemaSource.get_default()
            schema = None if source is None else source.lookup("org.gnome.desktop.interface",
                                                               True)
            if schema is not None and schema.has_key("color-scheme"):
                self._gnome = Gio.Settings.new("org.gnome.desktop.interface")
                self._gnome.connect("changed::color-scheme", lambda *_a: self._update())
        except Exception:  # noqa: BLE001
            self._gnome = None
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._update(force=True)

    @property
    def tokens(self) -> dict[str, str]:
        return DARK if self.dark else LIGHT

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def force(self, dark: bool | None) -> None:
        """Light (False) or dark (True) whatever the desktop says; None follows it again.
        For the screenshots and tests on a private display."""
        self._forced = dark
        self._update()

    def _wants_dark(self) -> bool:
        if self._forced is not None:
            return self._forced
        if gtk_prefers_dark(self._gtk.Settings.get_default()):
            return True
        if self._gnome is not None:
            try:
                return self._gnome.get_string("color-scheme") == "prefer-dark"
            except Exception:  # noqa: BLE001
                return False
        return False

    def _update(self, force: bool = False) -> None:
        dark = self._wants_dark()
        if dark == self.dark and not force:
            return
        self.dark = dark
        self._provider.load_from_data(css(self.tokens).encode())
        for fn in list(self._listeners):
            fn()


_THEME: Theme | None = None


def install() -> Theme:
    """The one Theme of this process, installed on first use."""
    global _THEME
    if _THEME is None:
        _THEME = Theme()
    return _THEME
