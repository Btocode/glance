from types import SimpleNamespace

from glance.daemon.test_overlay import handle_test_overlay
from glance.ui.strip_model import Segment, StripView


def test_only_with_glance_test_and_only_its_own_message():
    overlay = SimpleNamespace(strip_geometry=lambda: (1, 2, 3, 4), strip_text=lambda: "The",
                              panel_geometry=lambda: None)
    c = SimpleNamespace(strip=lambda: StripView((Segment("The", True),), 1))
    assert handle_test_overlay("test_overlay", {}, overlay, c, env={}) is None
    assert handle_test_overlay("status", {}, overlay, c, env={"GLANCE_TEST": "1"}) is None
    assert handle_test_overlay("test_overlay", {}, overlay, c, env={"GLANCE_TEST": "1"}) == {
        "ok": True, "strip": [1, 2, 3, 4], "strip_text": "The", "can_use": True,
        "panel": None, "enter_grabbed": False}
