from dataclasses import replace

from glance.config import GeneralCfg
from glance.daemon.scheduler import CheckScheduler
from glance.protocol import FieldSnapshot
from tests.fakes import FakeClock

TEXT40 = "The quick brown fox jumps over the lazy."  # 40 chars


def snap(text=TEXT40, caret=None, field_id="f1", gated=None, seq=1):
    return FieldSnapshot(
        app="app", wm_class="app", title="t", url=None, role="entry", text=text,
        caret=len(text) if caret is None else caret, caret_rect=None, prev_char_rect=None,
        field_rect=None, window_rect=None, field_id=field_id, capture_confidence=1.0,
        seq=seq, pid=1, gated=gated)


class Rig:
    def __init__(self, cfg=None):
        self.clock = FakeClock()
        self.cfg = cfg or GeneralCfg()
        self.calls: list[tuple] = []
        self.sched = CheckScheduler(
            self.clock, lambda: self.cfg,
            on_local=lambda s, g: self.calls.append(("local", g, self.clock.now())),
            on_settled=lambda s, g: self.calls.append(("settled", g, self.clock.now())),
            on_analyze=lambda s, g: self.calls.append(("analyze", g, self.clock.now())),
            on_typing=lambda: self.calls.append(("typing", None, self.clock.now())),
        )

    def kinds(self):
        return [c[0] for c in self.calls]


def test_scheduler_timeline():
    r = Rig()
    r.sched.on_snapshot(snap())
    assert r.kinds() == ["typing"]
    r.clock.advance(79)
    assert r.kinds() == ["typing"]
    r.clock.advance(1)
    assert r.kinds() == ["typing", "local"]
    r.clock.advance(320)  # t=400
    assert r.kinds() == ["typing", "local", "settled"]
    r.clock.advance(799)  # t=1199
    assert "analyze" not in r.kinds()
    r.clock.advance(1)  # t=1200
    assert r.kinds() == ["typing", "local", "settled", "analyze"]
    gens = {c[1] for c in r.calls if c[1] is not None}
    assert gens == {r.sched.generation} == {1}
    assert [c[2] for c in r.calls] == [0.0, 0.08, 0.4, 1.2]


def test_typing_restarts_all_timers_and_bumps_generation():
    r = Rig()
    for i in range(5):
        r.sched.on_snapshot(snap(TEXT40 + "x" * i, seq=i))
        r.clock.advance(100)
    assert r.sched.generation == 5
    assert r.kinds().count("typing") == 5
    assert "settled" not in r.kinds() and "analyze" not in r.kinds()
    # on_local fires at 80 ms of quiet; 100 ms steps exceed it, so it fired for the first four
    r.clock.advance(5000)
    assert r.kinds().count("settled") == 1 and r.kinds().count("analyze") == 1
    assert all(c[1] == 5 for c in r.calls if c[0] in ("settled", "analyze"))


def test_no_analyze_under_25_chars():
    r = Rig()
    r.sched.on_snapshot(snap("a" * 24))
    r.clock.advance(5000)
    assert "analyze" not in r.kinds() and "settled" in r.kinds()
    r.sched.on_snapshot(snap("a" * 25))
    r.clock.advance(5000)
    assert "analyze" in r.kinds()


BASE = "The quick brown fox jumps over the lazy dog"


def _analyzed_then(new_text):
    r = Rig()
    r.sched.on_snapshot(snap(BASE))
    r.clock.advance(5000)
    assert r.kinds().count("analyze") == 1
    r.sched.on_snapshot(snap(new_text))
    r.clock.advance(5000)
    return r.kinds().count("analyze")


def test_no_analyze_for_trivial_change():
    assert _analyzed_then(BASE + " fox") == 1       # one extra token
    assert _analyzed_then(BASE.replace("dog", "DOG")) == 1  # casefolded, identical multiset
    assert _analyzed_then(BASE + " fox now") == 2   # two extra tokens
    assert _analyzed_then(BASE.replace("dog", "cat")) == 2  # one token swapped = 2 differing


def test_trivial_change_compares_to_last_analyzed_not_last_seen():
    r = Rig()
    r.sched.on_snapshot(snap(BASE))
    r.clock.advance(5000)
    r.sched.on_snapshot(snap(BASE + " one"))
    r.clock.advance(5000)
    r.sched.on_snapshot(snap(BASE + " one two"))  # 2 vs the analyzed text
    r.clock.advance(5000)
    assert r.kinds().count("analyze") == 2


def test_analyzed_text_is_per_field():
    r = Rig()
    r.sched.on_snapshot(snap(BASE, field_id="a"))
    r.clock.advance(5000)
    r.sched.on_snapshot(snap(BASE, field_id="b"))
    r.clock.advance(5000)
    assert r.kinds().count("analyze") == 2


def test_caret_move_only_resettles():
    r = Rig()
    r.sched.on_snapshot(snap())
    r.clock.advance(5000)
    r.calls.clear()
    gen = r.sched.generation
    r.sched.on_snapshot(snap(caret=3))
    assert r.kinds() == []
    r.clock.advance(29)
    assert r.kinds() == []
    r.clock.advance(1)
    assert r.kinds() == ["settled"]
    assert r.sched.generation == gen and r.calls[0][1] == gen


def test_caret_moves_restart_the_30ms_settle():
    r = Rig()
    r.sched.on_snapshot(snap())
    r.clock.advance(5000)
    r.calls.clear()
    for c in (1, 2, 3):
        r.sched.on_snapshot(snap(caret=c))
        r.clock.advance(20)
    r.clock.advance(30)
    assert r.kinds() == ["settled"]


def test_caret_move_keeps_pending_analyze():
    r = Rig()
    r.sched.on_snapshot(snap())
    r.clock.advance(500)
    r.sched.on_snapshot(snap(caret=3))
    r.clock.advance(5000)
    assert r.kinds().count("analyze") == 1


def test_focus_lost_cancels_everything():
    r = Rig()
    r.sched.on_snapshot(snap())
    gen = r.sched.generation
    r.calls.clear()
    r.sched.on_focus_lost()
    r.clock.advance(5000)
    assert r.calls == [] and r.sched.generation == gen + 1


def test_gated_snapshot_cancels_everything():
    r = Rig()
    r.sched.on_snapshot(snap())
    gen = r.sched.generation
    r.calls.clear()
    r.sched.on_snapshot(snap("", gated="password"))
    r.clock.advance(5000)
    assert r.calls == [] and r.sched.generation == gen + 1


def test_refocus_same_text_is_a_fresh_check():
    r = Rig()
    r.sched.on_snapshot(snap())
    r.clock.advance(5000)
    r.sched.on_focus_lost()
    r.calls.clear()
    r.sched.on_snapshot(snap())
    r.clock.advance(5000)
    assert r.kinds()[:3] == ["typing", "local", "settled"]


def test_idle_change_uses_live_config():
    r = Rig()
    r.cfg = replace(r.cfg, chip_idle_ms=1000)
    r.sched.on_snapshot(snap())
    r.clock.advance(999)
    assert "settled" not in r.kinds()
    r.clock.advance(1)
    assert "settled" in r.kinds()


def test_analyze_idle_and_min_chars_use_live_config():
    r = Rig()
    r.cfg = replace(r.cfg, analyze_idle_ms=2000, min_chars_analyze=10)
    r.sched.on_snapshot(snap("a" * 12))
    r.clock.advance(1999)
    assert "analyze" not in r.kinds()
    r.clock.advance(1)
    assert "analyze" in r.kinds()


def test_caret_event_after_text_event_does_not_shorten_idle_wait():
    # each keystroke yields text-changed then caret-moved with the same text
    r = Rig()
    r.sched.on_snapshot(snap())
    r.clock.advance(10)
    r.sched.on_snapshot(snap(seq=2))
    r.clock.advance(100)
    assert "settled" not in r.kinds()
    r.clock.advance(300)  # past 400 ms from the first snapshot
    assert r.kinds().count("settled") == 1


def test_local_check_waits_longer_while_keys_keep_arriving():
    r = Rig()
    for i in range(6):  # ~6 keys/s: gaps of 166 ms
        r.sched.on_snapshot(snap(TEXT40 + "x" * i, seq=i))
        r.clock.advance(166)
    assert r.kinds().count("local") == 1  # only the burst's first key; no per-key Harper run
    r.clock.advance(250 - 166)
    assert r.kinds().count("local") == 2
    r.clock.advance(400)
    assert r.kinds().count("settled") == 1
    local_t = [c[2] for c in r.calls if c[0] == "local"][0]
    settled_t = [c[2] for c in r.calls if c[0] == "settled"][0]
    assert local_t < settled_t  # issues are in place before the settle
