"""Glance's look: one set of colour tokens, light and dark, and the GTK CSS built from them.

Every renderer reads these tokens: the GTK windows through `css()`, the GNOME Shell
extension through the scene glanced sends (`scene.tokens`), macOS through
`appkit_util.tokens()`. Dark is a slate UI with the brand emerald; light is its match on white,
with a deeper emerald ink for text and links (4.5:1) and the bright emerald kept for fills.

The pure part (tokens, contrast, CSS text, the brand mark's files) needs no display.
`install()` puts the CSS on the default screen and swaps it whenever the desktop switches
between light and dark (GTK's prefer-dark setting, its theme name, or GNOME's `color-scheme`).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

FONT = "Inter, Cantarell, 'Noto Sans', 'DejaVu Sans', sans-serif"
MONO = "'JetBrains Mono', 'DejaVu Sans Mono', 'Noto Sans Mono', monospace"

# The brand mark (docs/assets/brand/glance-mark.png, cut to squares without its glow; the
# small ones without the star): glance-mark-<px>.png.
ASSETS = Path(__file__).resolve().parent / "assets"
MARK_SIZES = (14, 16, 18, 20, 24, 28, 32, 36, 40, 48, 64, 128)


def mark_path(px: int) -> str:
    """The brand mark's file for `px` device pixels: the smallest one at least that big (the
    largest otherwise), so it is only ever scaled down, a little."""
    size = next((s for s in MARK_SIZES if s >= px), MARK_SIZES[-1])
    return str(ASSETS / f"glance-mark-{size}.png")


def _blend(fg: str, alpha: float, bg: str) -> str:
    """`fg` at `alpha` over `bg`, as one opaque #rrggbb (the tint some renderers cannot
    draw with alpha)."""
    f, b = _rgb(fg), _rgb(bg)
    return "#" + "".join(f"{round((alpha * x + (1 - alpha) * y) * 255):02x}" for x, y in zip(f, b))


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


LIGHT: dict[str, str] = {
    "surface": "#ffffff",       # the panel, the hover menu, the strip, the windows
    "card": "#f6f8fa",          # boxes on it: the suggested text, tone chips, the input
    "card_border": "#d8dee4",
    "hairline": "#d8dee4",      # the panel's border and its separators
    "strip": "#ffffff",         # the inline suggestion strip ...
    "strip_border": "#10B981",  # ... with its thin emerald border
    "glow": "rgba(16, 185, 129, 0.28)",  # and soft glow
    "text": "#17191c",
    "body": "#24292f",
    "muted": "#5a636e",
    "accent": "#0B7A52",        # emerald ink: text and links
    "accent_hover": "#086343",
    "accent_fill": "#10B981",   # emerald fill: the primary button, the count badge
    "accent_fill_hover": "#0ea371",
    "on_accent": "#062a1c",
    "highlight_rgba": "rgba(16, 185, 129, 0.14)",  # behind a changed word
    "hover": "#eef2f5",
    "keycap_bg": "#ffffff",
    "keycap_border": "#c9d1d9",
    "keycap_text": "#4a535e",
    "warn_bg": "#fbf1dc",
    "warn_border": "#efd9a6",
    "warn_text": "#6b4400",
    "shadow": "rgba(23, 25, 28, 0.16)",
    "dot_quiet": "#8a939d",
    "dot_warn": "#c98a12",
    "dot_info": "#0B7A52",
    "ghost": "#16875c",         # the completion after the caret, in italics
}

DARK: dict[str, str] = {
    "surface": "#151b23",
    "card": "#1b222c",
    "card_border": "#2a3340",
    "hairline": "#2a3340",
    "strip": "#151b23",
    "strip_border": "#3ECF8E",
    "glow": "rgba(62, 207, 142, 0.35)",
    "text": "#e6edf3",
    "body": "#d8e0e8",
    "muted": "#8b98a8",
    "accent": "#3ECF8E",
    "accent_hover": "#6ee0ad",
    "accent_fill": "#3ECF8E",
    "accent_fill_hover": "#5fdca4",
    "on_accent": "#0a1f15",
    "highlight_rgba": "rgba(62, 207, 142, 0.18)",
    "hover": "#212a35",
    "keycap_bg": "#1f2732",
    "keycap_border": "#3a4554",
    "keycap_text": "#c3ccd6",
    "warn_bg": "#3a2e17",
    "warn_border": "#5a4720",
    "warn_text": "#f2cf86",
    "shadow": "rgba(0, 0, 0, 0.5)",
    "dot_quiet": "#6b7787",
    "dot_warn": "#e0a43a",
    "dot_info": "#3ECF8E",
    "ghost": "#3ECF8E",
}

# The changed-word tint as one opaque colour on the box it sits in (the card).
LIGHT["highlight"] = _blend("#10B981", 0.14, LIGHT["card"])
DARK["highlight"] = _blend("#3ECF8E", 0.18, DARK["card"])

# (foreground, background) token pairs that carry text: each must reach 4.5:1.
TEXT_PAIRS = (
    ("body", "strip"), ("accent", "strip"), ("muted", "strip"),
    ("on_accent", "accent_fill"), ("on_accent", "accent_fill_hover"),
    ("text", "surface"), ("muted", "surface"), ("accent", "surface"),
    ("text", "card"), ("muted", "card"), ("accent", "card"),
    ("text", "highlight"), ("text", "hover"), ("muted", "hover"),
    ("keycap_text", "keycap_bg"), ("warn_text", "warn_bg"),
)


def rgb(tokens: dict[str, str], name: str) -> tuple[float, float, float]:
    """A token as cairo wants it (0..1 floats)."""
    return _rgb(tokens[name])


def rgba(value: str) -> tuple[float, float, float, float]:
    """`#rrggbb` or `rgba(r, g, b, a)` as cairo wants it."""
    s = value.strip()
    if s.startswith("rgba"):
        parts = [p.strip() for p in s[s.index("(") + 1:s.index(")")].split(",")]
        return (int(parts[0]) / 255, int(parts[1]) / 255, int(parts[2]) / 255, float(parts[3]))
    return (*_rgb(s), 1.0)


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

/* keycaps: Alt+Enter, Esc, 1-4, the return key, Tab */
.glance-root label.glance-kbd, .glance-kbd label {{
    font-family: {MONO}; font-size: 11px; color: {t['keycap_text']};
    background-color: {t['keycap_bg']}; border: 1px solid {t['keycap_border']};
    border-radius: 4px; padding: 0 5px; min-width: 9px; }}

/* the inline suggestion strip */
.glance-strip {{
    background-color: {t['strip']}; border: 1px solid {t['strip_border']};
    border-radius: 10px; padding: 7px 8px 7px 10px;
    box-shadow: inset 0 0 12px {t['glow']}; }}
.glance-strip.glance-plain {{ border-color: {t['hairline']}; box-shadow: none; }}
.glance-strip .glance-sentence {{ color: {t['body']}; font-size: 14px; }}
.glance-strip .glance-note {{ color: {t['muted']}; font-size: 13px; }}
.glance-strip .glance-guard {{ color: {t['warn_text']}; font-size: 13px; font-weight: 600; }}
.glance-strip.glance-warn {{ background-color: {t['warn_bg']};
                             border-color: {t['warn_border']}; box-shadow: none; }}
.glance-use {{
    background-color: {t['highlight_rgba']}; border: 1px solid {t['strip_border']};
    border-radius: 5px; padding: 2px 7px; }}
.glance-use label {{ font-family: {MONO}; font-size: 11.5px; color: {t['accent']};
                     font-weight: 600; }}
.glance-use:hover {{ background-color: {t['accent_fill']}; }}
.glance-use:hover label {{ color: {t['on_accent']}; }}
.glance-primary {{
    background-color: {t['accent_fill']}; color: {t['on_accent']}; border: none;
    border-radius: 7px; padding: 7px 14px; }}
.glance-primary label {{ color: {t['on_accent']}; font-size: 13px; font-weight: 600; }}
.glance-primary:hover {{ background-color: {t['accent_fill_hover']}; }}
.glance-quiet {{
    background-color: transparent; border: none; border-radius: 6px; padding: 2px 7px; }}
.glance-quiet label {{ color: {t['muted']}; font-size: 15px; font-weight: 600; }}
.glance-quiet:hover, .glance-quiet.glance-on {{ background-color: {t['hover']}; }}
.glance-quiet:hover label {{ color: {t['text']}; }}

/* the hover menu */
.glance-menu {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 10px; padding: 6px; box-shadow: 0 4px 14px {t['shadow']}; }}
.glance-menu .glance-note {{ color: {t['muted']}; font-size: 12px; padding: 4px 8px 6px 8px; }}
.glance-menu .glance-hint {{ padding: 6px 8px 2px 8px; border-top: 1px solid {t['hairline']}; }}
.glance-menu .glance-hint label.glance-meta {{ color: {t['muted']}; font-size: 12px; }}
.glance-item {{
    background-color: transparent; border: none; border-radius: 6px; padding: 6px 10px; }}
.glance-item label {{ color: {t['body']}; font-size: 13px; }}
.glance-item:hover {{ background-color: {t['hover']}; }}
.glance-item:hover label {{ color: {t['text']}; }}

/* the assistant panel (the drawer), select-anything's panel, the "Rewrite as…" entry */
.glance-drawer {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 12px; padding: 14px 16px;
    box-shadow: 0 8px 24px {t['shadow']}; }}
.glance-title {{ color: {t['text']}; font-size: 14px; font-weight: 600; }}
.glance-root label.glance-meta, .glance-meta label {{ color: {t['muted']}; font-size: 12px; }}
.glance-root label.glance-section {{ color: {t['muted']}; font-size: 12px; font-weight: 600; }}
.glance-suggest {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 8px; padding: 10px 12px; }}
.glance-suggest label {{ color: {t['text']}; font-size: 14px; }}
.glance-card {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 8px; padding: 8px 10px; }}
.glance-card:hover {{ border-color: {t['accent']}; background-color: {t['hover']}; }}
.glance-card .glance-card-title {{ color: {t['text']}; font-size: 13px; font-weight: 600; }}
.glance-card .glance-card-preview {{ color: {t['muted']}; font-size: 12px; }}
.glance-input {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 8px; padding: 6px 8px 6px 10px; }}
.glance-input label {{ color: {t['muted']}; font-size: 13px; }}
.glance-input label.glance-input-lead {{ color: {t['body']}; }}
.glance-input:hover {{ border-color: {t['accent']}; }}
.glance-root entry {{
    background-color: {t['card']}; color: {t['text']}; border: 1px solid {t['accent']};
    border-radius: 8px; padding: 7px 10px; font-size: 13px; box-shadow: none; }}
.glance-link {{
    background-color: transparent; border: none; border-radius: 6px; padding: 1px 4px; }}
.glance-link label {{ color: {t['muted']}; font-size: 12px; }}
.glance-link:hover label {{ color: {t['accent']}; }}
.glance-link.glance-strong label {{ color: {t['accent']}; font-weight: 600; }}
.glance-link.glance-strong:hover label {{ color: {t['accent_hover']}; }}
.glance-close {{
    background-color: transparent; border: none; border-radius: 6px; padding: 2px 4px; }}
.glance-close label {{ color: {t['muted']}; font-size: 12px; }}
.glance-close:hover {{ background-color: {t['hover']}; }}
.glance-close:hover label {{ color: {t['text']}; }}
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
    border-radius: 8px; padding: 8px 10px; }}
.glance-footer {{ border-top: 1px solid {t['hairline']}; padding-top: 10px; }}
.glance-row {{ border-top: 1px solid {t['hairline']}; padding-top: 8px; }}
.glance-excerpt {{ color: {t['text']}; }}

/* the "Connect an AI" window, the shortcuts and usage windows */
.glance-window {{ background-color: {t['surface']}; }}
.glance-window .glance-headline {{ color: {t['text']}; font-size: 18px; font-weight: 600; }}
.glance-window frame, .glance-window frame > border {{ border: none; }}
.glance-window .glance-card {{ padding: 12px 14px; }}
.glance-window entry, .glance-window combobox button {{ border-radius: 8px; }}
.glance-window .glance-plus {{ color: {t['muted']}; font-size: 12px; }}
.glance-window .glance-what {{ color: {t['body']}; font-size: 13px; }}
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
