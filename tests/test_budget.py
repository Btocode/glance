import json
import stat
from dataclasses import replace
from datetime import date

from glance.backends.budget import Budget
from glance.config import BackendCfg
from tests.fakes import FakeClock

NOV25 = date(2026, 11, 25)


def make(tmp_path, cfg=None, clock=None, today=lambda: NOV25):
    cfg = cfg or BackendCfg()
    return Budget(clock or FakeClock(), lambda: cfg, tmp_path / "usage.json", today)


def test_defaults_are_frugal():
    assert (BackendCfg().per_minute, BackendCfg().per_day) == (10, 300)


def test_per_minute_limit(tmp_path):
    clock = FakeClock()
    b = make(tmp_path, clock=clock)
    assert b.exhausted() is None
    assert [b.try_spend() for _ in range(10)] == [True] * 10
    assert b.try_spend() is False
    assert b.exhausted() == "minute"
    assert b.used_today() == 10  # a refused spend is not counted
    clock.advance(59_000)
    assert b.try_spend() is False
    clock.advance(1_000)
    assert b.exhausted() is None
    assert b.try_spend() is True


def test_per_minute_window_slides(tmp_path):
    clock = FakeClock()
    b = make(tmp_path, cfg=BackendCfg(per_minute=2), clock=clock)
    assert b.try_spend()
    clock.advance(30_000)
    assert b.try_spend()
    assert not b.try_spend()
    clock.advance(30_000)  # only the first spend has left the window
    assert b.try_spend()
    assert not b.try_spend()


def test_per_day_limit_shows_day(tmp_path):
    clock = FakeClock()
    b = make(tmp_path, cfg=BackendCfg(per_day=3), clock=clock)
    assert [b.try_spend() for _ in range(4)] == [True, True, True, False]
    assert b.exhausted() == "day"
    clock.advance(3_600_000)  # the minute window passing does not lift the daily cap
    assert b.try_spend() is False and b.exhausted() == "day"
    assert b.used_today() == 3


def test_day_wins_over_minute(tmp_path):
    b = make(tmp_path, cfg=BackendCfg(per_minute=2, per_day=2))
    assert b.try_spend() and b.try_spend()
    assert b.exhausted() == "day"


def test_budget_follows_config_changes(tmp_path):
    box = [BackendCfg(per_day=1)]
    b = Budget(FakeClock(), lambda: box[0], tmp_path / "usage.json", lambda: NOV25)
    assert b.try_spend() and not b.try_spend()
    box[0] = replace(box[0], per_day=2)
    assert b.exhausted() is None and b.try_spend()


def test_budget_day_rollover_and_corrupt_store(tmp_path):
    store = tmp_path / "usage.json"
    day = [NOV25]
    b = make(tmp_path, today=lambda: day[0])
    assert all(b.try_spend() for _ in range(3))
    # survives a restart on the same day
    b2 = make(tmp_path, today=lambda: day[0])
    assert b2.used_today() == 3
    # local date change resets, on a running instance and on a fresh one
    day[0] = date(2026, 11, 26)
    assert b2.used_today() == 0
    assert make(tmp_path, today=lambda: day[0]).used_today() == 0
    assert b2.try_spend()
    assert json.loads(store.read_text()) == {"date": "2026-11-26", "count": 1}
    # corrupt store fails closed: never unlimited
    store.write_text("{{{")
    b3 = make(tmp_path, today=lambda: day[0])
    assert b3.exhausted() == "day"
    assert b3.try_spend() is False
    assert b3.used_today() == 300
    assert json.loads(store.read_text()) == {"date": "2026-11-26", "count": 300}  # rewritten
    # ... and stays closed across another restart that day, then recovers the next day
    assert make(tmp_path, today=lambda: day[0]).exhausted() == "day"
    day[0] = date(2026, 11, 27)
    assert make(tmp_path, today=lambda: day[0]).try_spend() is True


def test_malformed_but_valid_json_fails_closed(tmp_path):
    store = tmp_path / "usage.json"
    for bad in ('[]', '{"date": "2026-11-25"}', '{"date": "2026-11-25", "count": -5}',
                '{"date": "2026-11-25", "count": "0"}', '{"date": "2026-11-25", "count": true}',
                '{"date": 20261125, "count": 0}', '{"date": "yesterday", "count": 0}', ''):
        store.write_text(bad)
        assert make(tmp_path).exhausted() == "day", bad
    store.write_bytes(b"\xff\xfe\x00")
    assert make(tmp_path).exhausted() == "day"


def test_missing_store_is_zero_and_parent_dir_is_created(tmp_path):
    store = tmp_path / "deep" / "glance" / "usage.json"
    b = Budget(FakeClock(), BackendCfg, store, lambda: NOV25)
    assert b.used_today() == 0 and b.exhausted() is None
    assert b.try_spend()
    assert json.loads(store.read_text()) == {"date": "2026-11-25", "count": 1}


def test_store_holds_only_date_and_count(tmp_path):
    store = tmp_path / "usage.json"
    b = make(tmp_path)
    b.try_spend()
    b.try_spend()
    data = json.loads(store.read_text())
    assert set(data) == {"date", "count"}
    assert data == {"date": "2026-11-25", "count": 2}
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["usage.json"]  # no temp file left behind


def test_unwritable_store_refuses_the_spend(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        b = Budget(FakeClock(), BackendCfg, locked / "usage.json", lambda: NOV25)
        assert b.exhausted() is None
        # a count that cannot be persisted would reset on every restart: fail closed
        assert b.try_spend() is False
        assert b.exhausted() == "day"
        assert b.used_today() == 0
        locked.chmod(0o700)
        assert b.try_spend() is True  # recovers as soon as the store is writable
        assert b.exhausted() is None and b.used_today() == 1
    finally:
        locked.chmod(0o700)


def test_unreadable_store_fails_closed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    b = Budget(FakeClock(), BackendCfg, blocker / "usage.json", lambda: NOV25)  # ENOTDIR
    assert b.exhausted() == "day" and b.try_spend() is False
