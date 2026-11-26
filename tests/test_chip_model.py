from glance.ui.chip_model import ChipInputs, ChipState, ChipView, chip_view


def V(**k):
    base = {"has_field": True, "settled": True, "text_len": 40}
    return chip_view(ChipInputs(**{**base, **k}))


def test_chip_priority():
    assert chip_view(ChipInputs()).state is ChipState.HIDDEN
    assert V(paused=True, n_local=3) == ChipView(ChipState.PAUSED, "Paused", None)
    assert V(gated="password") == ChipView(ChipState.PRIVATE, "Private", None)
    assert V(settled=False, n_local=3).state is ChipState.HIDDEN  # never while typing
    assert V(text_len=14, n_local=1).state is ChipState.HIDDEN
    assert V(snoozed=True, n_local=1).state is ChipState.HIDDEN
    assert V(n_local=3) == ChipView(ChipState.FIXES, "3", None)
    assert V(n_local=2, ai="none") == ChipView(ChipState.FIXES, "2", "none")
    assert V(n_local=2, ai="limit") == ChipView(ChipState.FIXES, "2", "limit")
    assert V(ai="none").state is ChipState.NO_AI and V(ai="limit").label == "AI paused (limit)"
    assert V(ai="none").label == "Connect an AI"
    assert V(mismatch=True, tone_label="Blunt", n_local=1, n_llm=2).label == "Blunt · 3"
    assert V(analyzing=True).state is ChipState.SPINNER and V().state is ChipState.IDLE


def test_private_and_paused_show_even_when_not_settled():
    assert V(paused=True, settled=False).state is ChipState.PAUSED
    assert V(gated="app", settled=False, text_len=0).state is ChipState.PRIVATE
    assert V(paused=True, gated="app").state is ChipState.PAUSED


def test_guard_beats_tone_and_fixes():
    v = V(guard="send", mismatch=True, tone_label="Blunt", n_local=2)
    assert v.state is ChipState.GUARD


def test_tone_count_omitted_when_zero():
    assert V(mismatch=True, tone_label="Warm").label == "Warm"


def test_min_chars_parameter_and_boundary():
    assert chip_view(ChipInputs(has_field=True, settled=True, text_len=15, n_local=1)).state is ChipState.FIXES
    assert chip_view(ChipInputs(has_field=True, settled=True, text_len=20, n_local=1), min_chars=30).state is ChipState.HIDDEN


def test_spinner_beats_fixes():
    assert V(analyzing=True, n_local=2).state is ChipState.SPINNER


def test_enum_complete():
    assert {s.name for s in ChipState} == {
        "HIDDEN", "IDLE", "FIXES", "SPINNER", "TONE", "PAUSED", "PRIVATE", "AI_LIMIT", "NO_AI", "GUARD"}

