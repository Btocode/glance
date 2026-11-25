import pytest

from glance.ui.theme import DARK, LIGHT, TEXT_PAIRS, contrast, css, gtk_prefers_dark


@pytest.mark.parametrize("tokens", [LIGHT, DARK], ids=["light", "dark"])
def test_every_text_colour_reaches_4_5_to_1(tokens):
    for fg, bg in TEXT_PAIRS:
        assert contrast(tokens[fg], tokens[bg]) >= 4.5, (fg, bg)


def test_both_themes_have_the_same_tokens_and_the_brief_s_light_values():
    assert LIGHT.keys() == DARK.keys()
    assert (LIGHT["strip"], LIGHT["accent"], LIGHT["hairline"]) == ("#edf6f3", "#0e6b5c",
                                                                    "#dfe3de")


def test_css_uses_the_tokens_and_the_type_scale():
    out = css(LIGHT)
    assert "#edf6f3" in out and "border-radius: 9px" in out and "border-radius: 12px" in out
    assert "font-size: 14px" in out and "font-size: 12px" in out and "Inter" in out
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
