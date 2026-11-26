"""The overlay scene the GNOME Shell extension renders: views and places as JSON. Pure.

Everything the extension needs to draw is decided here, in Python: which pieces show, their
text (Pango markup, escaped here), their colours (the theme's tokens), the brand mark's files,
the line glyphs and where everything goes. The extension only lays out widgets and moves them
along with the window.
"""
from __future__ import annotations

import dataclasses
from enum import Enum
from typing import Any

from glance.diffing import diff_markup
from glance.grammar.harper import SPELL_RULE
from glance.protocol import Rect
from glance.ui import look
from glance.ui.panel_model import PanelView, SuggestedBox
from glance.ui.status_model import StatusView
from glance.ui.strip_model import StripView
from glance.ui.theme import ASSETS, GLOW_PAD, MARK_SIZES

USE_LABEL = "Alt+Enter"
ASK_PLACEHOLDER = "e.g. make it a bullet list"


def _plain(value: Any) -> Any:
    """Dataclasses, enums and tuples as JSON-ready values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value


def strip_markup(view: StripView, accent: str) -> str:
    """The corrected text as Pango markup (the compact view, or the whole text once expanded:
    `view.shown`): the fixes semibold in the emerald ink."""
    return look.strip_markup(view.shown, accent)


def use_label(use_key: str) -> str:
    """The strip's keycap chip: the use key as it is pressed, "Alt+Enter" by default."""
    return look.chord_text(use_key)


def status_piece(view: StatusView, x: int, y: int) -> dict[str, Any]:
    """`look`: how the mark is drawn (`look.icon_look`); a pulsing glow takes its strength
    from LOOK["pulse"], one step per animation frame."""
    lk = look.icon_look(view.state.value, view.spin)
    return {"state": view.state.value, "badge": view.badge, "spin": view.spin,
            "message": view.message, "x": x, "y": y,
            "look": {"mark_alpha": lk.mark_alpha, "glow": lk.glow, "grey": lk.grey,
                     "pulse": lk.pulse, "glow_token": lk.glow_token,
                     "arc": lk.arc is not None}}


def strip_piece(view: StripView, x: int, y: int, w: int, tokens: dict[str, str],
                use_key: str, scroll_h: int = 0) -> dict[str, Any]:
    """`scroll`: the expanded whole text scrolls past `scroll_h` px (None: not expanded)."""
    plain = _plain(view)
    plain.pop("full", None)  # drawn through `markup` only
    return {"view": plain, "markup": strip_markup(view, tokens.get("accent", "#0e6b5c")),
            "can_use": view.can_use, "use_label": use_label(use_key), "x": x, "y": y, "w": w,
            # a check waiting for the AI: its loader line, and the keycap shown disabled
            "loading": view.loading,
            "use_disabled": view.loading is not None and bool(view.segments),
            "scroll": scroll_h if view.expanded and view.compact else None}


def suggested_piece(box: SuggestedBox, tokens: dict[str, str], use_key: str) -> dict[str, Any]:
    """The open panel's "Suggested" box (`panel_model.suggested_box`): the corrected text, or
    the selected tone's rewrite (`tone`, `working`); Apply is the `use` action either way."""
    return {"markup": look.suggested_markup(box.segments, tokens),
            "lines": list(box.lines), "can_use": box.can_apply,
            "apply_label": look.apply_label(use_key), "scroll": box.scroll,
            "tone": box.tone, "working": box.working, "placeholder": box.placeholder,
            "working_label": look.card_line(None, "loading"), "loading": box.loading}


def panel_piece(view: PanelView, x: int, y: int, w: int, follow: bool,
                suggested: dict[str, Any] | None = None,
                note: str | None = None) -> dict[str, Any]:
    """`suggested`: the corrected text while the field's panel is open (the strip is not
    drawn on its own then); `note`: the strip's line when it has no text to show."""
    plain = _plain(view)
    for row, issue in zip(plain["issues"], view.issues):
        row["spelling"] = issue.rule == SPELL_RULE
    for chip in plain["tone_chips"]:
        chip["glyph"] = look.tone_glyph(chip["key"])
        chip["line"] = look.card_line(chip["preview"], chip["state"])
        chip["skeleton"] = look.card_skeleton(view.loading, chip["preview"], chip["state"])
    has_tones = bool(view.tone_chips)
    return {"view": plain,
            "preview_markup": None if view.preview is None else diff_markup(view.preview),
            "title": look.TITLE, "subline": look.subline(view.mode, has_tones),
            "tone_line": look.tone_line(view.tone, view.tone_reason),
            "fixes_label": look.fixes_label(len(view.issues)),
            "suggested": suggested, "note": None if suggested else note,
            "x": x, "y": y, "w": w, "follow": follow}


def ghost_piece(text: str, x: int, y: int, font_px: int, max_width: int) -> dict[str, Any]:
    return {"text": text, "x": x, "y": y, "font_px": font_px, "max_width": max_width}


def window_ref(window: dict[str, Any] | None) -> dict[str, Any] | None:
    """The focused window the places were computed against: its id and frame."""
    if not window:
        return None
    return {"id": str(window.get("id", "")), "frame": list(window.get("frame") or [0, 0, 0, 0])}


def scene(tokens: dict[str, str], window: dict[str, Any] | None, *,
          status: dict[str, Any] | None = None, strip: dict[str, Any] | None = None,
          panel: dict[str, Any] | None = None, ghost: dict[str, Any] | None = None,
          sizes: dict[str, int] | None = None) -> dict[str, Any]:
    return {"v": 1, "tokens": dict(tokens), "window": window_ref(window), "status": status,
            "strip": strip, "panel": panel, "ghost": ghost, "sizes": dict(sizes or {}),
            "ask_placeholder": ASK_PLACEHOLDER, "look": LOOK}


# What the extension draws with besides the tokens: the brand mark's files and its glow's
# (glance-glow-<px>.png; it picks the size for the screen's scale), the working glow's
# breath, the line glyphs and the fixed words.
LOOK: dict[str, Any] = {
    "mark_dir": str(ASSETS), "mark_sizes": list(MARK_SIZES), "glow_pad": GLOW_PAD,
    "pulse": list(look.PULSE), "frames": look.FRAMES, "frame_ms": look.FRAME_MS,
    "still_glow": look.STILL_GLOW,
    "arc": {"sweep": look.ARC_SWEEP, "radius": look.ARC_RADIUS, "width": look.ARC_WIDTH},
    "skeleton": {"suggested": list(look.SKELETON_SUGGESTED), "card": list(look.SKELETON_CARD),
                 "bar_h": look.SKELETON_BAR_H, "gap": look.SKELETON_GAP},
    "glyphs": {name: [list(c) for c in path] for name, path in look.GLYPHS.items()},
    "words": {"suggested": look.SUGGESTED, "tone": look.TONE, "rewrite": look.REWRITE_LEAD,
              "return_key": look.RETURN_KEY, "esc": look.ESC_KEY, "close": look.CLOSE,
              "accept_key": look.ACCEPT_KEY, "fix_all": look.FIX_ALL},
}


def rects(raw: Any) -> list[Rect]:
    """Monitors as the extension reports them ([[x, y, w, h], ...]); junk is dropped."""
    out: list[Rect] = []
    for r in raw or ():
        try:
            x, y, w, h = (int(v) for v in r)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            out.append(Rect(x, y, w, h))
    return out
