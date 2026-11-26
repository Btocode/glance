"""The strip's key: Alt+Return is Glance's only while the strip offers a fix; plain Return
never is."""
from glance.daemon.links import FieldPanelOverlay, PanelEscape
from glance.protocol import Rect
from glance.ui.placement import Anchor
from glance.ui.strip_model import Segment, StripView
from tests.fakes import FakeHotkeys, FakeOverlay

FIX = StripView((Segment("The", True), Segment(" fox", False)), 1)
NOTE = StripView(note="Reads blunt for team channel — ⋯ for tone options")
AT = Anchor(caret=Rect(10, 10, 1, 18), field=Rect(0, 0, 400, 40), pid=7)


def test_return_is_grabbed_only_while_the_strip_offers_a_fix():
    overlay, keys, used = FakeOverlay(), FakeHotkeys(), []
    esc = PanelEscape(overlay, keys, lambda: None, on_use=lambda: used.append(1))
    esc.show_strip(FIX, AT)
    esc.show_strip(FIX, AT)  # every render calls it again
    assert keys.grabbed() == {"strip_use": "Alt+Return"}
    assert "Return" not in keys.grabbed().values()
    assert keys.log.count(("grab", "strip_use")) == 1
    assert (overlay.strip, overlay.strip_anchor) == (FIX, AT)
    keys.press("strip_use")
    assert used == [1]
    esc.show_strip(NOTE, AT)  # nothing to use: Alt+Return is the app's again
    assert keys.grabbed() == {} and overlay.strip == NOTE
    esc.show_strip(FIX, AT)
    esc.hide_strip()
    esc.hide_strip()
    assert keys.grabbed() == {} and overlay.strip is None
    assert keys.log.count(("ungrab", "strip_use")) == 2


def test_a_taken_return_still_shows_the_strip():
    overlay, keys = FakeOverlay(), FakeHotkeys(taken={"Alt+Return"})
    esc = PanelEscape(overlay, keys, lambda: None, on_use=lambda: None)
    esc.show_strip(FIX, AT)
    assert overlay.strip == FIX and keys.grabbed() == {}
    esc.hide_strip()
    assert overlay.strip is None


def test_without_a_use_callback_nothing_is_grabbed():
    overlay, keys = FakeOverlay(), FakeHotkeys()
    PanelEscape(overlay, keys, lambda: None).show_strip(FIX, AT)
    assert keys.grabbed() == {} and overlay.strip == FIX


def test_field_overlay_passes_the_strip_and_the_drawer_anchor_through():
    overlay = FakeOverlay()
    field = FieldPanelOverlay(overlay, lambda: False, lambda: None)
    field.show_strip(FIX, AT)
    assert overlay.strip == FIX
    from glance.ui.panel_model import PanelView

    field.show_panel(PanelView(), 1, 2, anchor=AT)
    assert overlay.panel_anchor == AT
    field.hide_strip()
    assert overlay.strip is None


def test_the_use_key_comes_from_the_config():
    overlay, keys = FakeOverlay(), FakeHotkeys()
    PanelEscape(overlay, keys, lambda: None, on_use=lambda: None,
                use_key=lambda: "Super+Return").show_strip(FIX, AT)
    assert keys.grabbed() == {"strip_use": "Super+Return"}


def test_the_default_use_key_is_alt_return():
    from glance.config import Config

    assert Config().ui.use_key == "Alt+Return"


def test_alt_return_is_also_grabbed_while_the_panel_offers_a_selected_tone():
    from glance.ui.panel_model import PanelView

    overlay, keys, used = FakeOverlay(), FakeHotkeys(), []
    esc = PanelEscape(overlay, keys, lambda: None, on_use=lambda: used.append(1))
    esc.show_panel(PanelView(tone="Blunt"), 0, 0, anchor=AT)  # no tone selected
    assert "strip_use" not in keys.grabbed()
    esc.show_panel(PanelView(preview_state="loading"), 0, 0, anchor=AT)  # applied when done
    assert keys.grabbed()["strip_use"] == "Alt+Return"
    esc.show_panel(PanelView(preview_state="ready", can_apply=True), 0, 0, anchor=AT)
    keys.press("strip_use")
    assert used == [1]
    esc.show_panel(PanelView(preview_state="ready", can_apply=False), 0, 0, anchor=AT)
    assert "strip_use" not in keys.grabbed()  # a copy only: nothing to apply
    esc.show_strip(FIX, AT)
    esc.show_panel(PanelView(preview_state="ready", can_apply=True), 0, 0, anchor=AT)
    esc.hide_panel()  # the strip still offers its fix
    assert keys.grabbed()["strip_use"] == "Alt+Return"
    esc.hide_strip()
    assert "strip_use" not in keys.grabbed()
    sel = PanelView(mode="selection", preview_state="ready", can_apply=True)
    esc.show_panel(sel, 0, 0)  # select-anything's panel: Copy / Paste, not Alt+Return
    assert "strip_use" not in keys.grabbed()
