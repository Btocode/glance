import inspect

import pytest

from glance.audience.profiles import BUILTIN
from glance.backends.base import Analysis
from glance.features import sendguard
from glance.features.sendguard import GUARD_LABELS, SendGuard, guard_reason, local_guard
from glance.grammar.store import IgnoredRules
from glance.ui.chip_model import ChipState
from tests.test_ai_analyze import CALM, Rig, snap

FILE = "I've attached the file for review now"


@pytest.mark.parametrize("text, want", [
    ("I've attached the report for you", "missing_attachment"),
    ("see the screenshot", "missing_attachment"),
    ("I've attached the report ￼", None),
    ("attached: https://x.test/report.pdf", None),
    ("here's the link", "missing_link"),
    ("here's the link https://x.test", None),
    ("link below", "missing_link"),
    ("she is very attached to her dog", "missing_attachment"),  # known false positive
    ("sounds good, thanks", None)])
def test_local_guard(text, want):
    assert local_guard(text) == want


@pytest.fixture
def ign(tmp_path):
    return IgnoredRules(tmp_path / "ignored.json")


def test_silenced_reason_is_skipped(ign):
    ign.ignore("guard:missing_attachment", "slack")
    assert guard_reason("I've attached it", None, BUILTIN["dm"], ign, "Slack") is None
    assert guard_reason("I've attached it", None, BUILTIN["dm"], ign, "discord") \
        == "missing_attachment"


def test_silenced_globally(ign):
    ign.ignore("guard:missing_attachment", None)
    assert guard_reason("I've attached it", None, BUILTIN["dm"], ign, "x") is None


def test_only_guard_profiles(ign):
    assert guard_reason("I've attached it", None, BUILTIN["email"], ign, "x") is None


def test_hostile_flag_wins(ign):
    a = Analysis("hostile", True, "insulting", (), ("hostile",))
    assert guard_reason("I've attached it you idiot", a, BUILTIN["team_channel"], ign,
                        "slack") == "hostile"


def test_flags_count_when_no_local_match(ign):
    a = Analysis("calm", False, None, (), ("missing_link",))
    assert guard_reason("see below", a, BUILTIN["dm"], ign, "slack") == "missing_link"
    a = Analysis("calm", False, None, (), ("missing_attachment", "missing_link"))
    assert guard_reason("see below", a, BUILTIN["dm"], ign, "slack") == "missing_attachment"


def test_silenced_hostile_falls_to_next(ign):
    ign.ignore("guard:hostile", None)
    a = Analysis("hostile", True, None, (), ("hostile",))
    assert guard_reason("I've attached it", a, BUILTIN["dm"], ign, "x") == "missing_attachment"


def test_chip_goes_amber_at_settle_without_ai(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    SendGuard(rig.c, rig.ai, IgnoredRules(tmp_path / "g.json")).attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(400)
    assert rig.overlay.chip.state is ChipState.GUARD
    assert rig.overlay.chip.label == GUARD_LABELS["missing_attachment"]
    assert rig.backend.calls == []


def test_attach_order_does_not_matter(tmp_path):
    rig = Rig(tmp_path, analysis=Analysis("blunt", True, "curt", ()))
    ign = IgnoredRules(tmp_path / "g.json")
    # Rig attached the AI first; the reverse order is the next test.
    SendGuard(rig.c, rig.ai, ign).attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(1200)
    assert rig.overlay.chip.state is ChipState.GUARD  # guard visible
    assert rig.ai.analysis().tone == "blunt"          # and the AI result still landed
    assert len(rig.c.extra_chip_inputs) == 2


def test_attach_before_ai_attach(tmp_path):
    rig = Rig(tmp_path)
    rig.c.extra_chip_inputs.clear()
    ign = IgnoredRules(tmp_path / "g.json")
    sg = SendGuard(rig.c, rig.ai, ign)
    sg.attach()
    rig.ai.attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(1200)
    assert rig.overlay.chip.state is ChipState.GUARD
    assert rig.ai.analysis() is not None


def test_hostile_appears_when_analysis_lands(tmp_path):
    rig = Rig(tmp_path, analysis=Analysis("hostile", True, "insulting", (), ("hostile",)))
    SendGuard(rig.c, rig.ai, IgnoredRules(tmp_path / "g.json")).attach()
    rig.c.handle_snapshot(snap("you are an absolute idiot honestly"))
    rig.clock.advance(400)
    assert rig.overlay.chip.state is not ChipState.GUARD
    rig.clock.advance(1000)
    assert rig.overlay.chip.state is ChipState.GUARD
    assert rig.overlay.chip.label == "Sounds hostile"


def test_guard_registers_no_key_grab():
    params = set(inspect.signature(SendGuard.__init__).parameters) - {"self"}
    assert params == {"controller", "ai", "ignored"}
    assert not [n for n in vars(sendguard) if "hotkey" in n.lower() or "grab" in n.lower()]
    assert not [m for m in ("on_key", "on_hotkey", "grab") if hasattr(SendGuard, m)]


def test_guard_clears_when_text_fixed(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    SendGuard(rig.c, rig.ai, IgnoredRules(tmp_path / "g.json")).attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(400)
    assert rig.overlay.chip.state is ChipState.GUARD
    rig.c.handle_snapshot(snap(FILE + " https://x.test/f.pdf", ))
    rig.clock.advance(400)
    assert rig.overlay.chip.state is not ChipState.GUARD


def test_silence_app_and_global(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    ign = IgnoredRules(tmp_path / "g.json")
    sg = SendGuard(rig.c, rig.ai, ign)
    sg.attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(400)
    assert sg.reason() == "missing_attachment"
    sg.silence("app")
    assert ign.is_ignored("guard:missing_attachment", "slack")
    assert not ign.is_ignored("guard:missing_attachment", "discord")
    assert rig.overlay.chip.state is not ChipState.GUARD
    assert sg.reason() is None


def test_silence_global(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    ign = IgnoredRules(tmp_path / "g.json")
    sg = SendGuard(rig.c, rig.ai, ign)
    sg.attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(400)
    sg.silence("global")
    assert ign.is_ignored("guard:missing_attachment", "anything")
    assert rig.overlay.chip.state is not ChipState.GUARD


def test_non_guard_profile_no_chip(tmp_path):
    rig = Rig(tmp_path, analysis=CALM)
    SendGuard(rig.c, rig.ai, IgnoredRules(tmp_path / "g.json")).attach()
    rig.c.handle_snapshot(snap(FILE, title="Re: report - Gmail", wm_class="Brave-browser"))
    rig.clock.advance(1200)
    assert rig.overlay.chip.state is not ChipState.GUARD


def test_strip_carries_the_guard_question(tmp_path):
    rig = Rig(tmp_path)
    SendGuard(rig.c, rig.ai, IgnoredRules(tmp_path / "g.json")).attach()
    rig.c.handle_snapshot(snap(FILE))
    rig.clock.advance(400)
    assert rig.overlay.strip.guard == "Attachment mentioned — attached?"
    assert not rig.overlay.strip.can_use
