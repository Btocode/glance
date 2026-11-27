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


# The status icon's glow (scripts/make-glow-assets.py): glance-glow-<px>.png is the mark at
# <px> blurred, white on a square GLOW_PAD * px bigger on every side, for tinting.
GLOW_PAD = 0.25


def _size(px: int) -> int:
    return next((s for s in MARK_SIZES if s >= px), MARK_SIZES[-1])


def mark_path(px: int) -> str:
    """The brand mark's file for `px` device pixels: the smallest one at least that big (the
    largest otherwise), so it is only ever scaled down, a little."""
    return str(ASSETS / f"glance-mark-{_size(px)}.png")


def glow_path(px: int) -> str:
    """The glow behind a mark `px` device pixels wide (the file is bigger by its pad)."""
    return str(ASSETS / f"glance-glow-{_size(px)}.png")


def _blend(fg: str, alpha: float, bg: str) -> str:
    """`fg` at `alpha` over `bg`, as one opaque #rrggbb (the tint some renderers cannot
    draw with alpha)."""
    f, b = _rgb(fg), _rgb(bg)
    return "#" + "".join(f"{round((alpha * x + (1 - alpha) * y) * 255):02x}" for x, y in zip(f, b))


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


LIGHT: dict[str, str] = {
    "surface": "#ffffff",       # the panel, the toolbars, the strip, the windows
    "card": "#f6f8fa",          # boxes on it: the suggested text, tone chips, the input
    "card_border": "#d8dee4",
    "hairline": "#d8dee4",      # the panel's border and its separators
    "strip": "#ffffff",         # the inline suggestion strip ...
    "strip_border": "#d8dee4",  # ... with a hairline border (no glow in the light theme)
    "strip_shadow": "rgba(23, 25, 28, 0.10)",  # and a soft neutral shadow (8 px blur)
    "use_border": "#10B981",    # the strip's Alt+Enter chip
    "icon_glow": "rgba(16, 185, 129, 0.12)",  # behind the status icon's mark: very faint
    "icon_working": "rgba(16, 185, 129, 1.0)",  # the working glow (look.PULSE is its alpha)
    "skeleton": "#e7ebef",  # loading placeholder bars
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
    "shadow": "rgba(23, 25, 28, 0.12)",
    "dot_quiet": "#8a939d",
    "dot_warn": "#c98a12",
    "dot_info": "#0B7A52",
    "ghost": "#16875c",         # the completion after the caret, in italics
    "page": "#f6f8fa",          # the Settings window behind its groups ...
    "group": "#ffffff",         # ... and the groups themselves
    "switch_off": "#c9d1d9",
}

DARK: dict[str, str] = {
    "surface": "#151b23",
    "card": "#1b222c",
    "card_border": "#2a3340",
    "hairline": "#2a3340",
    "strip": "#151b23",
    "strip_border": "rgba(62, 207, 142, 0.45)",  # a 1 px emerald line, no bloom
    "strip_shadow": "rgba(62, 207, 142, 0.12)",  # at most a whisper of one (8 px blur)
    "use_border": "#3ECF8E",
    "icon_glow": "rgba(62, 207, 142, 0.30)",
    "icon_working": "rgba(62, 207, 142, 1.0)",
    "skeleton": "#28313b",
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
    "shadow": "rgba(0, 0, 0, 0.32)",
    "dot_quiet": "#6b7787",
    "dot_warn": "#e0a43a",
    "dot_info": "#3ECF8E",
    "ghost": "#3ECF8E",
    "page": "#151b23",
    "group": "#1b222c",
    "switch_off": "#3a4554",
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
    ("text", "page"), ("muted", "page"), ("text", "group"), ("muted", "group"),
    ("accent", "group"), ("warn_text", "group"),
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
.glance-root {{ background-color: transparent; background-image: none; }}
.glance-skeleton {{ background-color: {t['skeleton']}; border-radius: 5px; }}
.glance-root button.glance-use:disabled {{ opacity: 0.45; }}
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
    border-radius: 10px; padding: 7px 8px 7px 10px; box-shadow: none; }}
.glance-strip.glance-plain {{ border-color: {t['hairline']}; box-shadow: none; }}
.glance-strip .glance-sentence {{ color: {t['body']}; font-size: 14px; }}
.glance-strip .glance-note {{ color: {t['muted']}; font-size: 13px; }}
.glance-strip .glance-guard {{ color: {t['warn_text']}; font-size: 13px; font-weight: 600; }}
.glance-strip.glance-warn {{ background-color: {t['warn_bg']};
                             border-color: {t['warn_border']}; box-shadow: none; }}
.glance-use {{
    background-color: {t['highlight_rgba']}; border: 1px solid {t['use_border']};
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

/* the toolbars' card (the status icon's, the selection's) and their items */
.glance-menu {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 10px; padding: 6px; box-shadow: none; }}  /* no room around it for one: a cut shadow is a dark band */
.glance-menu .glance-note {{ color: {t['muted']}; font-size: 12px; padding: 4px 8px 6px 8px; }}
.glance-menu .glance-hint {{ padding: 6px 8px 2px 8px; border-top: 1px solid {t['hairline']}; }}
.glance-menu .glance-hint label.glance-meta {{ color: {t['muted']}; font-size: 12px; }}
.glance-item {{
    background-color: transparent; border: none; border-radius: 6px; padding: 6px 10px; }}
.glance-item label {{ color: {t['body']}; font-size: 13px; }}
.glance-item:hover {{ background-color: {t['hover']}; }}
.glance-item:hover label {{ color: {t['text']}; }}

/* selection actions: the action bar beside selected text */
.glance-selmark {{ background-color: transparent; border: none; padding: 0; }}
.glance-selbar {{ padding: 4px; border-radius: 10px; }}
.glance-selbtn {{ padding: 5px 8px; }}
.glance-selbtn label {{ font-size: 13px; }}
.glance-selmore {{ border-top: 1px solid {t['hairline']}; padding-top: 4px; }}
.glance-selbar .glance-note {{ padding: 0 8px 0 2px; }}
.glance-selbtn label.glance-keyhint {{ color: {t['muted']}; font-size: 11px; }}
.glance-strip label.glance-strip-label {{
    color: {t['accent']}; font-size: 12px; font-weight: 600; }}

/* Glance Shot's card: the tabs (one row, always in the same place) and the body */
.glance-tabs {{ background-color: {t['group']}; border: 1px solid {t['card_border']};
    border-radius: 9px; padding: 3px; }}
.glance-tabs button.glance-tab {{
    background-color: transparent; background-image: none; border: none; box-shadow: none;
    border-radius: 7px; padding: 5px 12px; color: {t['muted']}; font-size: 13px; }}
.glance-tabs button.glance-tab:hover {{ color: {t['text']}; }}
.glance-tabs button.glance-tab.glance-selected {{
    background-color: {t['highlight_rgba']}; color: {t['accent']}; font-weight: 600; }}
.glance-tabs button.glance-tab.glance-muted {{ opacity: 0.5; }}
.glance-shot-body {{ background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 8px; padding: 12px; }}
.glance-shot-body scrolledwindow, .glance-shot-body undershoot,
.glance-shot-body overshoot {{ background: none; border: none; box-shadow: none; }}
button.glance-tool {{ background-color: transparent; background-image: none; border: 1px solid
    {t['card_border']}; box-shadow: none; border-radius: 7px; padding: 3px 9px;
    color: {t['text']}; font-size: 12px; }}
button.glance-tool:hover {{ border-color: {t['accent']}; }}
.glance-status {{ color: {t['accent']}; font-size: 12px; }}
.glance-status.glance-warn {{ color: {t['warn_text']}; }}

/* the assistant panel (the drawer), select-anything's panel, the "Rewrite as…" entry */
.glance-drawer {{
    background-color: {t['surface']}; border: 1px solid {t['hairline']};
    border-radius: 12px; padding: 14px 16px;
    box-shadow: 0 4px 12px {t['shadow']}; }}
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
.glance-card.glance-selected, .glance-card.glance-selected:hover {{
    border-color: {t['accent_fill']}; background-color: {t['highlight']}; }}
.glance-suggest.glance-selected {{ border-color: {t['accent_fill']}; }}
.glance-root label.glance-tone-tag {{ color: {t['accent']}; font-size: 12px; font-weight: 600; }}
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

/* Glance Shot: the bar after a capture, its result card */
.glance-shot .glance-secondary.glance-muted {{ background-color: transparent; }}
.glance-shot .glance-secondary.glance-muted label {{ color: {t['muted']}; }}
.glance-done {{
    background-color: {t['highlight']}; border: 1px solid {t['card_border']};
    border-radius: 9px; padding: 8px 10px; }}
.glance-done label {{ color: {t['text']}; font-size: 13px; }}
.glance-result {{
    background-color: {t['card']}; border: 1px solid {t['card_border']};
    border-radius: 8px; padding: 10px 12px; }}
.glance-result label {{ color: {t['text']}; font-size: 13.5px; }}

/* the "Connect an AI" window, the shortcuts and usage windows */
.glance-window {{ background-color: {t['surface']}; }}
.glance-window .glance-headline {{ color: {t['text']}; font-size: 18px; font-weight: 600; }}
.glance-window frame, .glance-window frame > border {{ border: none; }}
.glance-window .glance-card {{ padding: 12px 14px; }}
.glance-window entry, .glance-window combobox button {{ border-radius: 8px; }}
.glance-window .glance-plus {{ color: {t['muted']}; font-size: 12px; }}
.glance-window .glance-what {{ color: {t['body']}; font-size: 13px; }}

/* the Settings window: a section list on the left, groups of rows on the right */
.glance-settings {{ background-color: {t['page']}; }}
.glance-settings .glance-nav {{ border-right: 1px solid {t['hairline']}; padding: 16px 10px; }}
.glance-nav-item {{
    background-color: transparent; border: none; border-radius: 7px; padding: 7px 12px; }}
.glance-nav-item label {{ color: {t['body']}; font-size: 13.5px; }}
.glance-nav-item:hover {{ background-color: {t['hover']}; }}
.glance-nav-item.glance-on {{ background-color: {t['highlight']}; }}
.glance-nav-item.glance-on label {{ color: {t['text']}; font-weight: 600; }}
.glance-settings .glance-headline {{ color: {t['text']}; font-size: 20px; font-weight: 600; }}
.glance-group {{
    background-color: {t['group']}; border: 1px solid {t['card_border']};
    border-radius: 10px; }}
.glance-set-row {{ padding: 12px 16px; }}
.glance-set-row.glance-sep {{ border-top: 1px solid {t['hairline']}; }}
.glance-root label.glance-set-title {{ color: {t['text']}; font-size: 13.5px; }}
.glance-root label.glance-set-help {{ color: {t['muted']}; font-size: 12px; }}
.glance-root label.glance-set-error {{ color: {t['warn_text']}; font-size: 12px;
                                       font-weight: 600; }}
.glance-root label.glance-set-ok {{ color: {t['accent']}; font-size: 12px; }}
.glance-root label.glance-tag {{
    color: {t['muted']}; background-color: {t['page']}; border: 1px solid {t['card_border']};
    border-radius: 6px; padding: 1px 7px; font-size: 12px; }}
.glance-root label.glance-tag.glance-on {{
    color: {t['accent']}; border-color: {t['accent']}; }}
.glance-settings switch {{
    background-color: {t['switch_off']}; background-image: none; border: none;
    border-radius: 12px; min-height: 22px; min-width: 40px; box-shadow: none;
    color: transparent; font-size: 0; }}
.glance-settings switch:checked {{ background-color: {t['accent_fill']}; }}
.glance-settings switch:disabled {{ opacity: 0.45; }}
.glance-settings switch slider {{
    background-color: #ffffff; background-image: none; border: none; border-radius: 11px;
    min-width: 18px; min-height: 18px; margin: 2px; box-shadow: 0 1px 2px {t['shadow']}; }}
.glance-seg button {{
    background-color: {t['group']}; border: 1px solid {t['card_border']}; border-radius: 0;
    padding: 5px 12px; margin-left: -1px; }}
.glance-seg button:first-child {{ border-radius: 7px 0 0 7px; margin-left: 0; }}
.glance-seg button:last-child {{ border-radius: 0 7px 7px 0; }}
.glance-seg button:only-child {{ border-radius: 7px; }}
.glance-seg button label {{ color: {t['body']}; font-size: 12.5px; }}
.glance-seg button:hover {{ background-color: {t['hover']}; }}
.glance-seg button.glance-on {{ background-color: {t['accent_fill']};
                                border-color: {t['accent_fill']}; }}
.glance-seg button.glance-on label {{ color: {t['on_accent']}; font-weight: 600; }}
.glance-settings entry {{
    background-color: {t['page']}; border: 1px solid {t['card_border']}; min-height: 0;
    padding: 5px 9px; }}
.glance-settings button:disabled {{ opacity: 0.45; }}
.glance-settings .glance-secondary {{ padding: 5px 11px; }}
.glance-settings popover {{
    background-color: transparent; border: none; box-shadow: none; padding: 0; }}
.glance-settings .glance-item.glance-on label {{ color: {t['accent']}; font-weight: 600; }}
.glance-settings entry:focus {{ border-color: {t['accent']}; }}
.glance-settings entry.glance-bad {{ border-color: {t['dot_warn']}; }}
.glance-settings combobox button {{
    background-color: {t['page']}; background-image: none; border: 1px solid
    {t['card_border']}; border-radius: 7px; padding: 3px 8px; box-shadow: none; }}
.glance-settings combobox button label {{ color: {t['text']}; font-size: 12.5px; }}
.glance-settings button.glance-capture {{
    background-color: {t['page']}; border: 1px solid {t['card_border']}; border-radius: 7px;
    padding: 4px 8px; }}
.glance-settings button.glance-capture:hover {{ border-color: {t['accent']}; }}
.glance-settings button.glance-capture.glance-on {{
    border-color: {t['accent_fill']}; background-color: {t['highlight']}; }}
.glance-settings button.glance-capture.glance-on label {{ color: {t['accent']}; font-size: 12.5px; }}
.glance-settings .glance-banner {{ margin-bottom: 2px; }}
.glance-root label.glance-mono {{ font-family: {MONO}; font-size: 12px; color: {t['body']}; }}
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
