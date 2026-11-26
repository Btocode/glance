import json
import stat
from datetime import date, timedelta

import pytest

from glance.backends.api import ApiBackend, _api_usage
from glance.backends.base import BackendError
from glance.backends.claude import ClaudeBackend, claude_usage
from glance.backends.codex import _final_message
from glance.backends.usage import (
    SUBSCRIPTION_NOTE,
    Usage,
    UsageLedger,
    fmt_tokens,
    make_usage,
    render_report,
    summary_line,
    tray_lines,
)
from glance.config import BackendCfg, load_config
from tests.test_supervisor_backend import CTX, Rig, StubBackend, reason_of


def ledger(tmp_path, day=date(2026, 11, 25)):
    box = [day]
    led = UsageLedger(tmp_path / "usage-ledger.json", lambda: box[0])
    return led, box


# ---------------------------------------------------------------- ledger
def test_ledger_aggregates_per_backend_model_and_keeps_no_text(tmp_path):
    led, _ = ledger(tmp_path)
    led.record("claude", "haiku", "analyze", False, "ok", Usage(100, 20, 0.01))
    led.record("claude", "haiku", "sharpen", True, "failed", None)
    led.record("api", "m", "complete", False, "cancelled", Usage(5, 6, None))
    p = led.periods()["today"]
    rows = {(r["backend"], r["model"]): r for r in p["rows"]}
    c = rows[("claude", "haiku")]
    assert (c["calls"], c["failed"], c["input_tokens"], c["cost_usd"]) == (2, 1, 100, 0.01)
    assert c["purposes"] == {"analyze": 1, "sharpen": 1} and c["speculative"] == 1
    assert rows[("api", "m")]["cost_usd"] is None and rows[("api", "m")]["failed"] == 0
    assert p["total"]["calls"] == 3 and p["total"]["cost_usd"] == 0.01
    mode = stat.S_IMODE((tmp_path / "usage-ledger.json").stat().st_mode)
    assert mode == 0o600
    assert set(json.loads((tmp_path / "usage-ledger.json").read_text())) == {"version", "days"}


def test_unreported_cost_is_unknown_not_zero(tmp_path):
    led, _ = ledger(tmp_path)
    led.record("gemini", "flash", "rewrite", False, "ok", None)
    row = led.periods()["today"]["rows"][0]
    assert row["cost_usd"] is None and row["tokens_known"] is False
    assert led.month_cost() == 0.0


def test_periods_prune_and_reload(tmp_path):
    led, box = ledger(tmp_path)
    led.record("claude", "haiku", "analyze", False, "ok", Usage(1, 1, 0.5))
    box[0] = date(2026, 11, 28)
    led.record("claude", "haiku", "analyze", False, "ok", Usage(1, 1, 0.25))
    per = led.periods()
    assert per["today"]["total"]["calls"] == 1
    assert per["last_7_days"]["total"]["calls"] == 2
    assert led.month_cost() == 0.75
    assert led.periods(2)["last_2_days"]["total"]["calls"] == 1
    box[0] = date(2026, 12, 1)
    assert led.month_cost() == 0.0  # a new month starts again
    box[0] = date(2026, 11, 28) + timedelta(days=91)
    led.record("claude", "haiku", "analyze", False, "ok", None)
    assert list(json.loads((tmp_path / "usage-ledger.json").read_text())["days"]) == [
        box[0].isoformat()]
    assert UsageLedger(tmp_path / "usage-ledger.json", lambda: box[0]).periods()[
        "today"]["total"]["calls"] == 1


def test_corrupt_store_starts_empty_and_is_kept(tmp_path):
    path = tmp_path / "usage-ledger.json"
    path.write_text("{nope")
    led = UsageLedger(path, lambda: date(2026, 11, 25))
    assert led.periods()["today"]["rows"] == []
    assert (tmp_path / "usage-ledger.json.bad").exists()


def test_unwritable_store_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    led = UsageLedger(blocker / "sub" / "l.json", lambda: date(2026, 11, 25))
    led.record("claude", "haiku", "analyze", False, "ok", None)  # no exception
    assert led.periods()["today"]["total"]["calls"] == 1


# ---------------------------------------------------------------- parsing
def test_make_usage_ignores_junk():
    assert make_usage("5", True, -1) is None
    assert make_usage(3, None, 0.5) == Usage(3, None, 0.5)


def test_claude_usage_counts_cache_and_cost():
    obj = {"usage": {"input_tokens": 3, "cache_creation_input_tokens": 10,
                     "cache_read_input_tokens": 7, "output_tokens": 4},
           "total_cost_usd": 0.0123}
    assert claude_usage(obj) == Usage(20, 4, 0.0123)
    assert claude_usage({"result": "x"}) is None


def test_claude_stream_reports_usage_from_result():
    got = []
    lines = iter([json.dumps({"type": "result", "result": "Hi",
                              "usage": {"input_tokens": 2, "output_tokens": 1},
                              "total_cost_usd": 0.001})])
    assert "".join(ClaudeBackend._parse_stream(lines, got.append)) == "Hi"
    assert got == [Usage(2, 1, 0.001)]


def test_base_backend_usage_accumulates_and_resets():
    b = ClaudeBackend(runner=None)
    b._add_usage(Usage(1, 2, None))
    b._add_usage(Usage(3, None, 0.5))
    assert b.take_usage() == Usage(4, 2, 0.5)
    assert b.take_usage() is None


def test_codex_turn_completed_usage():
    lines = [json.dumps({"type": "item.completed",
                         "item": {"type": "agent_message", "text": "ok"}}),
             json.dumps({"type": "turn.completed",
                         "usage": {"input_tokens": 50, "cached_input_tokens": 10,
                                   "output_tokens": 7}})]
    got = []
    assert _final_message(iter(lines), got.append) == "ok"
    assert got == [Usage(50, 7, None)]


def test_api_usage_shapes_and_stream_events():
    assert _api_usage({"input_tokens": 4, "output_tokens": 5}) == Usage(4, 5, None)
    assert _api_usage({"prompt_tokens": 1, "completion_tokens": 2}) == Usage(1, 2, None)
    assert _api_usage("x") is None
    api = ApiBackend(keys=None, provider="anthropic")
    api._note_stream_usage({"type": "message_start", "message": {"usage": {"input_tokens": 9,
                                                                          "output_tokens": 1}}})
    api._note_stream_usage({"type": "message_delta", "usage": {"output_tokens": 12}})
    assert api._stream_tokens == [9, 12]


# ---------------------------------------------------------------- supervisor
class Reporting(StubBackend):
    model = "haiku"

    def __init__(self, name, usage=None, **kw):
        super().__init__(name, **kw)
        self._u = usage

    def take_usage(self):
        u, self._u = self._u, None
        return u

    def analyze(self, text, ctx):
        self._u = Usage(10, 2, 0.5) if self._u is None else self._u
        return super().analyze(text, ctx)


def rig(tmp_path, **kw):
    r = Rig(tmp_path, BackendCfg(choice="claude", enabled=()), backends={"claude": Reporting("claude")})
    r.sup.ledger = UsageLedger(tmp_path / "ledger.json", lambda: date(2026, 11, 25))
    for k, v in kw.items():
        setattr(r.sup, k, v)
    return r


def test_supervisor_records_every_call_by_purpose(tmp_path):
    r = rig(tmp_path)
    r.sup.analyze("a", CTX)
    assert "".join(r.sup.rewrite("a", "friendlier", CTX)) == "Hi there"
    "".join(r.sup.rewrite("a", "sharpen_prompt", CTX))
    "".join(r.sup.complete("a", CTX, speculative=False))
    row = r.sup.ledger.periods()["today"]["rows"][0]
    assert row["calls"] == 4 and row["model"] == "haiku"
    assert row["purposes"] == {"analyze": 1, "rewrite": 1, "sharpen": 1, "complete": 1}
    assert row["cost_usd"] == 0.5 and row["input_tokens"] == 10
    today = r.sup.usage_today()
    assert today["calls"] == 4 and today["tokens"] == 12
    assert today["budget_remaining"] == 300 - 4 and today["limit"] is None


def test_failed_call_is_counted_failed(tmp_path):
    r = rig(tmp_path)
    r.backends["claude"].fail = "unavailable"
    with pytest.raises(Exception):
        r.sup.analyze("a", CTX)
    row = r.sup.ledger.periods()["today"]["rows"][0]
    assert (row["calls"], row["failed"]) == (1, 1)


def test_monthly_cost_limit_refuses_new_calls_only_when_reached(tmp_path):
    r = rig(tmp_path)
    r.sup._cost_limit = lambda: 1.0
    r.sup.analyze("a", CTX)  # cost 0.5: below
    assert r.sup.status() == "ok" and r.sup.limit_reason() is None
    r.sup.analyze("b", CTX)  # cost 1.0: reached
    assert r.sup.status() == "limit" and r.sup.limit_reason() == "month"
    calls = r.backends["claude"].calls
    assert reason_of(lambda: r.sup.analyze("c", CTX)) == "limit"
    assert reason_of(lambda: list(r.sup.rewrite("c", "friendlier", CTX))) == "limit"
    assert r.sup.test("claude") == (False, "limit")
    assert r.backends["claude"].calls == calls
    rep = r.sup.usage_report()
    assert rep["limit_reason"] == "month" and rep["month_cost_usd"] == 1.0
    assert tray_lines(rep)[-1] == "Monthly AI cost limit reached"


def test_no_limit_by_default_and_config_parses(tmp_path):
    p = tmp_path / "c.toml"
    assert load_config(p).ai.monthly_cost_limit_usd is None
    p.write_text("[ai]\nmonthly_cost_limit_usd = 5\n")
    assert load_config(p).ai.monthly_cost_limit_usd == 5.0
    p.write_text("[ai]\nmonthly_cost_limit_usd = -1\n")
    with pytest.raises(Exception):
        load_config(p)


# ---------------------------------------------------------------- presentation
def test_formats_and_render(tmp_path):
    assert fmt_tokens(12_345) == "12.3k" and fmt_tokens(999) == "999"
    r = rig(tmp_path)
    r.sup.analyze("a", CTX)
    rep = r.sup.usage_report()
    assert summary_line("Today", rep["periods"]["today"]["total"]) == \
        "Today: 1 call · 12 tokens · $0.50"
    text = render_report(rep)
    assert "claude / haiku" in text and "1 (0)" in text and "of 300" in text
    assert SUBSCRIPTION_NOTE in text
    api_only = {"periods": {"today": {"start": "2026-11-25", "end": "2026-11-25",
                "rows": [{"backend": "api", "model": "m", "calls": 1, "failed": 0,
                          "input_tokens": 1, "output_tokens": 1, "tokens_known": True,
                          "cost_usd": 0.01, "purposes": {}, "speculative": 0}],
                "total": {"calls": 1, "failed": 0, "input_tokens": 1, "output_tokens": 1,
                          "tokens_known": True, "cost_usd": 0.01}}}}
    assert SUBSCRIPTION_NOTE not in render_report(api_only)


def test_cli_usage_json_and_text_through_a_fake_daemon(monkeypatch, capsys):
    from glance import cli

    rep = {"ok": True, "id": 1, "periods": {}, "budget": {"used_today": 2, "per_day": 300,
                                                          "per_minute": 10},
           "monthly_cost_limit_usd": None, "month_cost_usd": 0.0, "limit_reason": None}
    seen = []
    monkeypatch.setattr(cli, "_request", lambda t, p, timeout_s=3.0: seen.append((t, p)) or rep)
    assert cli.main(["usage", "--days", "3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["budget"]["used_today"] == 2
    assert seen == [("usage", {"days": 3})]
    assert cli.main(["usage"]) == 0
    assert "Calls today: 2 of 300" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["usage", "--days", "0"])


def test_cli_usage_without_daemon_reads_the_ledger(tmp_path, monkeypatch, capsys):
    from glance import cli

    for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(k, str(tmp_path / k))
    monkeypatch.setattr(cli, "_request", lambda *a, **k: None)
    assert cli.main(["usage", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["periods"]["today"]["rows"] == [] and out["budget"]["per_day"] == 300
