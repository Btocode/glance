"""Latency gates (spec §8.3) on a private Xvfb, against the real glanced with timing probes
(`timed_glanced`) and the GTK test app. Fake backend unless marked `paid`.

    .venv/bin/pytest -m xvfb tests/acceptance/test_latency.py -v -s

Each test prints its measured numbers (`-s`). One glanced per display, so every test starts and
stops its own.
"""
from __future__ import annotations

import os
import random
import statistics
import threading
import time

import pytest

from tests.acceptance.harness import Typist
from tests.acceptance.perf import (
    HARPER,
    TimedApp,
    TimedGlanced,
    comm,
    pct,
    summary,
    type_paced,
    wait_for,
    windows_of,
)

TEXT = "Teh quick brown fox jumps over the lazy dog."
SPECULATE = ("[backend]\nspeculate = true\nlive_autocomplete = true\n"
             "per_minute = 1000\nper_day = 100000\n")  # the default budget is 10 calls a minute
WORDS = ("teh", "recieve", "definately", "seperate", "occured", "wich", "adress", "freind")

pytestmark = [
    pytest.mark.xvfb,
    pytest.mark.skipif(not HARPER.exists(), reason="harper-ls is not installed"),
]


@pytest.fixture
def typist(session):
    t = Typist(session)
    yield t
    t.close()


def ready(g: TimedGlanced, text_len: int | None = None) -> None:
    assert wait_for(lambda: g.status().get("capture_connected")
                    and g.status().get("ai") == "ok"
                    and (text_len is None or (g.status().get("field") or {}).get("len")
                         == text_len), 40.0), (g.status(), g.log()[-3000:])


def test_local_fix_latency(session, typist, tmp_path):
    with TimedApp(session) as app, TimedGlanced(session, tmp_path / "g") as g:
        ready(g)
        rng = random.Random(1)
        ok = 0
        for i in range(50):
            seen = g.count("chip_shown")
            typist.chord("Control_L", "a")
            typist.type(f"I will {rng.choice(WORDS)} the report from {rng.choice(WORDS)} today. ")
            assert g.wait_count("chip_shown", seen, 5.0), (i, g.log()[-1500:])
            ok += g.state().get("chip", {}).get("state") == "fixes"
        shown = g.timings("chip_shown", "ms_since_settle")
        since_text = g.timings("chip_shown", "ms_since_text")
        print("\nchip_shown ms_since_settle", summary(shown))
        print("chip_shown ms_since_text (last keystroke -> chip)", summary(since_text))
        print("chips with fixes at the last check:", ok, "of 50")
        assert len(shown) >= 50 and ok >= 45
        assert pct([max(0.0, v) for v in shown], 95) <= 150
        assert app.buffer().endswith("today. ")


def test_chip_reposition(session, typist, tmp_path):
    with TimedApp(session, text=TEXT) as app, TimedGlanced(session, tmp_path / "g") as g:
        ready(g, len(TEXT))
        assert wait_for(lambda: g.state().get("chip", {}).get("state") == "fixes", 15.0)
        time.sleep(1.0)
        typist.chord("Control_L", "Home")
        time.sleep(0.6)
        seen = len(g.timings("chip_moved"))
        for key in ["Right"] * 40 + ["Left"] * 40 + ["Right"] * 20:  # 100 moves, every one
            typist.key(key)  # to a new place (the end of the text anchors the same chip)
            time.sleep(0.2)
        time.sleep(0.5)
        moved = g.timings("chip_moved")[seen:]
        print("\nchip_moved ms", summary(moved))
        work = g.timings("chip_moved", "ms_since_settle")[seen:]
        print("chip_moved ms_since_settle (the move itself, after the 50 ms debounce)",
              summary(work))
        assert len(moved) >= 80, (len(moved), g.log()[-1500:])
        assert pct(work, 95) <= 50
        assert app.buffer() == TEXT


def test_pregenerated_apply(session, typist, tmp_path):
    with TimedApp(session, text=TEXT, undo=True) as app, \
            TimedGlanced(session, tmp_path / "g", config=SPECULATE) as g:
        ready(g, len(TEXT))
        for i in range(15):
            time.sleep(3.6)  # SPECULATE_IDLE_MS (3 s) + the pre-generation
            seen = g.count("apply_sent")
            typist.chord("Super_L", "Alt_L", "1")
            assert g.wait_count("apply_sent", seen, 5.0), (i, g.log()[-1500:])
            assert wait_for(lambda: app.buffer() == "Thanks so much, will do!", 5.0)
            typist.chord("Control_L", "z")  # the rewrite was pasted: one undo restores
            assert wait_for(lambda: app.buffer() == TEXT, 5.0), app.buffer()
            time.sleep(0.3)
        got = g.timings("apply_sent", "ms_since_request")
        print("\napply_sent ms_since_request", summary(got))
        assert len(got) == 15 and pct(got, 95) <= 200


def test_tab_never_stolen(session, typist, tmp_path):
    env = {"GLANCE_FAKE_COMPLETE": "none"}
    with TimedApp(session, accepts_tab=True) as app, \
            TimedGlanced(session, tmp_path / "g", config=SPECULATE, env=env) as g:
        ready(g)
        typist.type("Dear team, a note about the plan: ")  # long enough for a live completion
        time.sleep(1.5)
        before = app.buffer().count("\t")
        for _ in range(1000):
            typist.key("Tab")
        time.sleep(1.0)
        got = app.buffer().count("\t") - before
        print("\ntabs delivered", got, "of 1000; ghost windows shown:", g.count("ghost_shown"))
        assert got == 1000
        assert g.count("ghost_shown") == 0


def _key_deltas(session, typist, glance: bool, tmp_path) -> list[float]:
    keys = ("abcdefghijklmnopqrstuvwxyz " * 80)[:2000]
    with TimedApp(session) as app:
        if glance:
            with TimedGlanced(session, tmp_path / "g") as g:
                ready(g)
                time.sleep(2.0)
                type_paced(typist, keys, 0.020)
                time.sleep(0.5)
        else:
            time.sleep(2.0)
            type_paced(typist, keys, 0.020)
            time.sleep(0.5)
        return list(app.deltas)


def test_no_added_keystroke_latency(session, typist, tmp_path):
    bare = _key_deltas(session, typist, False, tmp_path / "a")
    with_glance = _key_deltas(session, typist, True, tmp_path / "b")
    print("\nkey -> buffer changed, Glance stopped:", summary(bare))
    print("key -> buffer changed, Glance running:", summary(with_glance))
    assert len(bare) > 1900 and len(with_glance) > 1900
    d50 = statistics.median(with_glance) - statistics.median(bare)
    d99 = pct(with_glance, 99) - pct(bare, 99)
    print(f"difference of medians {d50:.3f} ms, of p99 {d99:.3f} ms")
    assert d50 < 1.0
    assert d99 < 5.0


def test_no_ui_while_typing(session, typist, tmp_path):
    with TimedApp(session) as app, TimedGlanced(session, tmp_path / "g") as g:
        ready(g)
        time.sleep(1.0)
        baseline = len(windows_of(session, g.proc.pid))
        seen: list[int] = []
        stop = threading.Event()

        def sample() -> None:
            t0 = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - t0 > 0.5:  # the first keys' own hide is in flight
                    seen.append(len(windows_of(session, g.proc.pid)) - baseline)
                time.sleep(0.02)

        th = threading.Thread(target=sample)
        th.start()
        # 6 keys/s, a sentence with a misspelling so a chip would be due, for 20 s
        phrase = "I will recieve the report from teh team today and then reply soon, "
        type_paced(typist, (phrase * 3)[:120], 1 / 6)
        stop.set()
        th.join()
        print("\nsamples", len(seen), "max extra windows", max(seen))
        assert len(seen) >= 500 and max(seen) <= 0
        assert app.buffer().startswith("I will")


# ----------------------------------------------------------------------------- live ghost


def test_live_ghost_latency_fake_api(session, typist, tmp_path):
    with TimedApp(session), \
            TimedGlanced(session, tmp_path / "g", config=SPECULATE) as g:
        ready(g)
        for i in range(15):
            seen = g.count("ghost_shown")
            typist.chord("Control_L", "a")
            typist.type("Could you please send me the notes ")
            assert g.wait_count("ghost_shown", seen, 6.0), (i, g.log()[-1500:])
            typist.key("Escape")
            time.sleep(0.3)
        got = g.timings("ghost_shown", "ms_since_pause")
        print("\nghost_shown ms_since_pause (last keystroke -> ghost, idle wait included)",
              summary(got))
        assert statistics.median(got) <= 1200


# ----------------------------------------------------------------------------- paid


def _real(session, tmp_path, config: str) -> TimedGlanced:
    return TimedGlanced(session, tmp_path / "g", config=config, fake=False,
                        env={"PATH": os.environ["PATH"]})


def _count(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@pytest.mark.paid
def test_analyze_p50_cli(session, typist, tmp_path):
    n = _count("GLANCE_PAID_ANALYZE", 10)
    warm = os.environ.get("GLANCE_PAID_WARM", "1") != "0"  # 0: a cold `claude -p` per call
    cfg = f'[backend]\nchoice = "claude"\nwarm = {"true" if warm else "false"}\n'
    with TimedApp(session), _real(session, tmp_path, cfg) as g:
        ready(g)
        time.sleep(8.0 if warm else 0.0)  # the warm process starts in the background
        for i in range(n):
            seen = g.count("analyze_done")
            typist.chord("Control_L", "a")
            typist.type(f"Please send the quarterly figure number {i} to the finance team today. ")
            assert g.wait_count("analyze_done", seen, 60.0), g.log()[-1500:]
        got = g.timings("analyze_done")
        print(f"\nanalyze_done ms (claude CLI, {'warm' if warm else 'cold'})", got, summary(got))
        print("processes:", [(p, comm(p)) for p in g.pids(workers=True)])
        assert statistics.median(got) <= 3500


@pytest.mark.paid
def test_rewrite_first_token_cli(session, typist, tmp_path):
    n = _count("GLANCE_PAID_REWRITE", 5)
    with TimedApp(session), _real(session, tmp_path, '[backend]\nchoice = "claude"\n') as g:
        ready(g)
        typist.type("please send the quarterly figures to the finance team today")
        # The tone hotkey needs the audience the analysis resolves: wait for the first one.
        assert g.wait_count("analyze_done", 0, 60.0), g.log()[-1500:]
        for i in range(n):
            seen = g.count("rewrite_first_delta")
            typist.chord("Super_L", "Alt_L", "1")
            assert g.wait_count("rewrite_first_delta", seen, 60.0), g.log()[-1500:]
            typist.chord("Control_L", "z")
            time.sleep(1.0)
        got = g.timings("rewrite_first_delta")
        print("\nrewrite_first_delta ms (claude CLI)", summary(got))
        assert statistics.median(got) <= 3500


@pytest.mark.paid
@pytest.mark.skipif(not os.environ.get("GLANCE_API_KEY_SET"),
                    reason="no API key (set GLANCE_API_KEY_SET=1 with a key in the keyring)")
def test_api_latencies(session, typist, tmp_path):
    cfg = '[backend]\nchoice = "api"\n'
    with TimedApp(session), _real(session, tmp_path, cfg) as g:
        ready(g)
        for i in range(10):
            seen = g.count("analyze_done")
            typist.chord("Control_L", "a")
            typist.type(f"Please send the quarterly figure number {i} to the finance team. ")
            assert g.wait_count("analyze_done", seen, 30.0)
        for _ in range(5):
            seen = g.count("rewrite_first_delta")
            typist.chord("Super_L", "Alt_L", "1")
            assert g.wait_count("rewrite_first_delta", seen, 30.0)
            typist.chord("Control_L", "z")
            time.sleep(1.0)
        for _ in range(10):
            seen = g.count("ghost_shown")
            typist.chord("Control_L", "a")
            typist.type("Could you please send me the notes ")
            assert g.wait_count("ghost_shown", seen, 30.0)
            typist.key("Escape")
        a, r, c = (statistics.median(g.timings(e, f)) for e, f in (
            ("analyze_done", "ms"), ("rewrite_first_delta", "ms"), ("ghost_shown", "ms_since_pause")))
        print("\napi p50 analyze/rewrite/ghost", a, r, c)
        assert a <= 1200 and r <= 1000 and c <= 1200
