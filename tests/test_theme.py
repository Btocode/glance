import pytest

from pathlib import Path

from glance.ui.theme import (
    DARK,
    LIGHT,
    MARK_SIZES,
    TEXT_PAIRS,
    contrast,
    css,
    gtk_prefers_dark,
    mark_path,
    rgba,
)


@pytest.mark.parametrize("tokens", [LIGHT, DARK], ids=["light", "dark"])
def test_every_text_colour_reaches_4_5_to_1(tokens):
    for fg, bg in TEXT_PAIRS:
        assert contrast(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)


def test_both_themes_have_the_same_tokens_and_the_design_s_values():
    assert LIGHT.keys() == DARK.keys()
    # dark: the slate UI with the brand emerald
    assert (DARK["surface"], DARK["card"], DARK["hairline"], DARK["text"], DARK["muted"],
            DARK["accent"]) == ("#151b23", "#1b222c", "#2a3340", "#e6edf3", "#8b98a8",
                                "#3ECF8E")
    assert DARK["highlight_rgba"] == "rgba(62, 207, 142, 0.18)"
    assert DARK["glow"] == "rgba(62, 207, 142, 0.35)"
    # light: emerald fill with dark text; a deeper emerald ink for text and links
    assert (LIGHT["surface"], LIGHT["card"], LIGHT["hairline"], LIGHT["text"],
            LIGHT["muted"], LIGHT["accent_fill"], LIGHT["accent"]) == (
        "#ffffff", "#f6f8fa", "#d8dee4", "#17191c", "#5a636e", "#10B981", "#0B7A52")
    assert LIGHT["highlight_rgba"] == "rgba(16, 185, 129, 0.14)"


def test_the_tint_is_the_accent_over_the_card():
    assert DARK["highlight"] == "#21413e" and LIGHT["highlight"] == "#d6efe9"
    assert rgba(DARK["glow"]) == (62 / 255, 207 / 255, 142 / 255, 0.35)
    assert rgba("#ffffff") == (1.0, 1.0, 1.0, 1.0)


def test_the_brand_mark_is_shipped_at_every_size_and_picked_up_never_down():
    for size in MARK_SIZES:
        assert Path(mark_path(size)).is_file(), size
    assert mark_path(16).endswith("glance-mark-16.png")
    assert mark_path(17).endswith("glance-mark-18.png")  # scaled down, never up
    assert mark_path(4000).endswith("glance-mark-128.png")


def test_css_uses_the_tokens_and_the_type_scale():
    out = css(LIGHT)
    assert "#10B981" in out and "border-radius: 10px" in out and "border-radius: 12px" in out
    assert "font-size: 14px" in out and "font-size: 12px" in out and "Inter" in out
    assert ".glance-kbd" in out and "monospace" in out and "inset 0 0 12px" in out
    for sheet in (out, css(DARK)):
        assert sheet.count("{") == sheet.count("}") > 20


class Settings:
    def __init__(self, dark=False, name="Adwaita"):
        self.values = {"gtk-application-prefer-dark-theme": dark, "gtk-theme-name": name}

    def get_property(self, name):
        return self.values[name]


def test_dark_follows_gtk():
    assert gtk_prefers_dark(None) is False
    assert gtk_prefers_dark(Settings()) is False
    assert gtk_prefers_dark(Settings(dark=True)) is True
    assert gtk_prefers_dark(Settings(name="Yaru-dark")) is True
    assert gtk_prefers_dark(Settings(name="Adwaita:dark")) is True
