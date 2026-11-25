# ruff: noqa: F401, F811  (fixtures are imported from the sibling test modules)
"""Fast, cheap default models; changing them; the claude warm process; unknown-model retry."""
import json
from datetime import date

import pytest

from glance import cli
from glance.backends.base import (
    UNKNOWN_MODEL,
    BackendError,
    BaseBackend,
    Ctx,
    Health,
    error_detail,
)
from glance.backends.budget import Budget
from glance.backends.claude import ClaudeBackend
from glance.backends.codex import CodexBackend
from glance.backends.proc import ProcResult
from glance.backends.supervisor import BackendSupervisor
from glance.config import API_DEFAULT_MODEL, DEFAULT_MODELS, BackendCfg, load_config
from glance.ui.onboarding_model import build_rows, effective_model, model_text
from tests.fakes import FakeClock
from tests.test_backend_ctl import rig
from tests.test_claude_backend import CTX, log, make, records, runtime
from tests.test_cli_backend import NONE_READY, fake_ctl

VALID = {"tone": "casual", "mismatch": False, "reason": None, "issues": [], "flags": []}


# ---------------------------------------------------------------- the defaults table
def test_defaults_are_the_cheap_tier_in_one_table():
    assert DEFAULT_MODELS == {"claude": "haiku", "codex": "gpt-6-luna", "gemini": "flash-lite"}
    assert "opencode" not in DEFAULT_MODELS  # the user's own default
    assert API_DEFAULT_MODEL == "claude-haiku-5-5"
    assert BackendCfg().models == DEFAULT_MODELS


def test_user_model_overrides_one_entry_only(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[backend.models]\ncodex = "gpt-x"\n')
    assert load_config(p).backend.models == {**DEFAULT_MODELS, "codex": "gpt-x"}


def test_api_default_lives_in_the_table_only():
    from glance.backends import api

    assert api.DEFAULT_ANTHROPIC_MODEL is API_DEFAULT_MODEL


def test_effective_model():
    assert effective_model("claude", BackendCfg()) == "haiku"
    assert effective_model("opencode", BackendCfg()) == ""
    assert effective_model("api", BackendCfg()) == API_DEFAULT_MODEL
    assert effective_model("api", BackendCfg(api_provider="openai")) == ""
    assert effective_model("api", BackendCfg(api_model="m")) == "m"
    assert model_text("", "opencode") == "tool default"
    assert model_text("", "api").startswith("not set")


def test_rows_carry_the_model():
    rows = build_rows({}, None, {"claude": "haiku"})
    assert next(r for r in rows if r.name == "claude").model == "haiku"
    assert next(r for r in rows if r.name == "codex").model == ""


# ---------------------------------------------------------------- ctl + cli
def test_backend_list_reports_models(rig):
    reply = rig.send("backend_list")
    assert reply["models"] == {"claude": "haiku", "codex": "gpt-6-luna", "gemini": "flash-lite",
                               "opencode": "", "api": API_DEFAULT_MODEL}


def test_backend_model_set_and_default(rig):
    assert rig.send("backend_model", {"name": "codex", "model": "gpt-x"}) == {
        "ok": True, "name": "codex", "model": "gpt-x", "default": False}
    assert load_config(rig.cfg_path).backend.models["codex"] == "gpt-x"
    assert rig.send("backend_list")["models"]["codex"] == "gpt-x"
    reply = rig.send("backend_model", {"name": "codex"})
    assert reply == {"ok": True, "name": "codex", "model": "gpt-6-luna", "default": True}
    assert load_config(rig.cfg_path).backend.models["codex"] == "gpt-6-luna"
    assert "gpt-x" not in rig.cfg_path.read_text()


def test_backend_model_api_writes_api_model(rig):
    rig.send("backend_model", {"name": "api", "model": "gpt-4o-mini"})
    assert load_config(rig.cfg_path).backend.api_model == "gpt-4o-mini"
    rig.send("backend_model", {"name": "api"})
    assert load_config(rig.cfg_path).backend.api_model == ""


@pytest.mark.parametrize("bad", ["--flag", "a b", "", "x" * 201, "a;b", 5, "-m"])
def test_backend_model_rejects_odd_names(rig, bad):
    assert rig.send("backend_model", {"name": "claude", "model": bad}) == {
        "ok": False, "error": "bad model"}
    assert rig.cfg_path.read_text() == ""


def test_backend_model_unknown_backend(rig):
    assert rig.send("backend_model", {"name": "nope", "model": "x"})["error"] == "unknown backend"


def test_cli_model_set_and_default(fake_ctl, capsys):
    fake_ctl.replies["backend_model"] = {"name": "codex", "model": "gpt-x", "default": False}
    assert cli.main(["backend", "model", "codex", "gpt-x"]) == 0
    assert fake_ctl.seen[-1] == ("backend_model", {"name": "codex", "model": "gpt-x"})
    assert "codex model: gpt-x" in capsys.readouterr().out
    fake_ctl.replies["backend_model"] = {"name": "codex", "model": "gpt-6-luna", "default": True}
    assert cli.main(["backend", "model", "codex", "--default"]) == 0
    assert fake_ctl.seen[-1] == ("backend_model", {"name": "codex", "model": None})
    assert "(default)" in capsys.readouterr().out


def test_cli_model_needs_exactly_one_of_model_or_default(fake_ctl, capsys):
    assert cli.main(["backend", "model", "codex"]) == 2
    assert cli.main(["backend", "model", "codex", "x", "--default"]) == 2
    assert fake_ctl.seen == []


def test_cli_list_shows_the_model(fake_ctl, capsys):
    fake_ctl.replies["backend_list"] = {**NONE_READY, "models": {"claude": "haiku"}}
    assert cli.main(["backend", "list"]) == 0
    out = capsys.readouterr().out
    assert "model: haiku" in out and "model: tool default" in out


# ---------------------------------------------------------------- unknown model retry
def test_error_detail_recognises_rejected_models():
    for text in ('{"error":{"code":"model_not_found"}}', "Unknown model: foo",
                 "The model `x` does not exist", "ModelNotFoundError",
                 "Requested entity was not found. invalid model name"):
        assert error_detail(text, "d") == UNKNOWN_MODEL, text
    for text in ("rate limit exceeded", "please log in", ""):
        assert error_detail(text, "d") == "d"


class Picky(BaseBackend):
    """Rejects any explicit model, serves the CLI default."""
    name = "picky"

    def __init__(self, model="m1"):
        self.model, self.seen = model, []

    def _run_json(self, system, user, schema, timeout_s):
        self.seen.append(self.model)
        if self.model:
            raise BackendError("unavailable", UNKNOWN_MODEL)
        return VALID

    def _run_stream(self, system, user, timeout_s):
        self.seen.append(self.model)
        if self.model:
            raise BackendError("unavailable", UNKNOWN_MODEL)
        yield "ok"

    def cancel(self):
        pass

    def health(self):
        return Health(True, True, "")


def test_rejected_model_retries_once_without_it(monkeypatch):
    logged = []
    monkeypatch.setattr("glance.backends.base.log_event", lambda *a, **k: logged.append((a, k)))
    b = Picky()
    b.set_model("m1")
    b.analyze("hi", CTX)
    assert b.seen == ["m1", ""]
    assert logged == [(("model_rejected",), {"backend": "picky"})]  # type only, no name
    b.analyze("hi", CTX)
    assert b.seen == ["m1", "", ""]  # not retried with m1 again
    b.set_model("m1")  # same config value: still dropped
    assert b.model == ""
    b.set_model("m2")  # the user changed it: try again
    assert b.model == "m2"


def test_rejected_model_on_a_stream_retries_when_nothing_was_sent():
    b = Picky()
    assert list(b.rewrite("hi", "professional", CTX)) == ["ok"]
    assert b.seen == ["m1", ""]


def test_other_errors_do_not_drop_the_model():
    class Down(Picky):
        def _run_json(self, *a):
            self.seen.append(self.model)
            raise BackendError("unavailable", "boom")

    b = Down()
    with pytest.raises(BackendError):
        b.analyze("hi", CTX)
    assert b.seen == ["m1"] and b.model == "m1"


class FakeRunner:
    def __init__(self, results):
        self.results, self.argvs = list(results), []

    def run(self, argv, stdin, timeout_s, env=None):
        self.argvs.append(argv)
        return self.results.pop(0)

    def cancel(self):
        pass


def test_codex_unknown_model_retries_without_dash_m():
    err = ProcResult(
        json.dumps({"type": "turn.failed", "error": {"message": "model_not_found: nope"}}),
        "", 1)
    ok = ProcResult(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps(VALID)}}), "", 0)
    runner = FakeRunner([err, ok])
    CodexBackend(runner, "nope").analyze("hi", CTX)
    assert "-m" in runner.argvs[0] and "nope" in runner.argvs[0]
    assert "-m" not in runner.argvs[1]


# ---------------------------------------------------------------- warm process
class Pool:
    """Backend double with the supervisor's per-call hooks."""

    def __init__(self, name):
        self.name, self.models, self.warm = name, [], []

    def set_model(self, m):
        self.models.append(m)

    def set_warm(self, on):
        self.warm.append(on)

    def health(self):
        return Health(True, True, "")


def supervisor(tmp_path, cfg, names=("claude", "codex")):
    clock = FakeClock()
    main = {n: Pool(n) for n in names}
    spec = {n: Pool(n) for n in names}
    sup = BackendSupervisor(
        main, lambda: cfg, Budget(clock, lambda: cfg, tmp_path / "u.json", lambda: date(2026, 11, 25)),
        clock, lambda: False, lambda s: None, speculative=spec,
        config_path=tmp_path / "config.toml")
    return sup, main, spec


def test_warm_is_on_for_claude_when_chosen_by_default(tmp_path):
    sup, main, spec = supervisor(tmp_path, BackendCfg(choice="claude"))
    sup.sync()
    assert main["claude"].warm == [True]
    assert main["codex"].warm == [False]
    assert spec["claude"].warm == [False]  # the speculative twin never warms


def test_warm_off_when_another_backend_is_chosen_or_config_says_false(tmp_path):
    sup, main, _ = supervisor(tmp_path, BackendCfg(choice="codex"))
    sup.sync()
    assert main["claude"].warm == [False]
    sup, main, _ = supervisor(tmp_path, BackendCfg(choice="claude", warm=False))
    sup.sync()
    assert main["claude"].warm == [False]
    sup, main, _ = supervisor(tmp_path, BackendCfg(choice=None))
    sup.sync()
    assert main["claude"].warm == [False]


def test_switching_backend_tears_warm_down_at_once(tmp_path):
    sup, main, _ = supervisor(tmp_path, BackendCfg(choice="claude"))
    sup.sync()
    sup.use("codex")  # config not reloaded yet: use() applies the new choice itself
    assert main["claude"].warm == [True, False]


def test_models_follow_the_config(tmp_path):
    sup, main, spec = supervisor(tmp_path, BackendCfg(choice="claude"))
    sup.sync()
    assert main["claude"].models == ["haiku"] and main["codex"].models == ["gpt-6-luna"]
    assert spec["codex"].models == ["gpt-6-luna"]


def test_real_claude_backend_warm_lifecycle(log):
    b = make(log)  # fresh: off
    assert not b._warm_on
    b.set_warm(True)  # turning it on starts the idle processes by itself
    b.wait_warm()
    assert set(b._idle) == {"analyze", "rewrite"}
    proc = b._idle["analyze"]
    assert proc.alive
    b.set_warm(False)  # switched away: the idle processes are killed
    assert b._idle == {} and not proc.alive
    b.wait_warm()
    assert b._idle == {}


def test_supervisor_with_claude_chosen_warms_then_second_call_uses_it(tmp_path, log):
    clock = FakeClock()
    cfg = BackendCfg(choice="claude")  # warm left at its default
    claude = make(log)
    sup = BackendSupervisor(
        {"claude": claude}, lambda: cfg,
        Budget(clock, lambda: cfg, tmp_path / "u.json", lambda: date(2026, 11, 25)),
        clock, lambda: False, lambda s: None, config_path=tmp_path / "config.toml")
    sup.sync()
    claude.wait_warm()
    assert set(claude._idle) == {"analyze", "rewrite"}  # an idle process before any call
    sup.analyze("hello", CTX)  # first call is already warm
    claude.wait_warm()
    sup.analyze("again", CTX)  # and so is the second, from the replacement
    claude.wait_warm()
    recs = records(log)
    assert len(recs) == 2 and all("--input-format" in r["argv"] for r in recs)
    claude.set_warm(False)


def test_model_change_restarts_the_idle_process(log):
    b = make(log, warm=True)
    b.set_model("haiku")
    b.prewarm("analyze")
    b.wait_warm()
    proc = b._idle["analyze"]
    b.set_model("haiku")
    assert proc.alive  # unchanged: kept
    b.set_model("sonnet")
    b.wait_warm()
    assert not proc.alive and b.model == "sonnet" and b._idle["analyze"] is not proc
    b.close()


def test_rejected_model_is_not_passed_to_the_retry(log):
    b = make(log, warm=True)
    b.set_model("nope")
    b._drop_model(BackendError("unavailable", UNKNOWN_MODEL))
    b.wait_warm()
    assert "--model" not in b.argv_json("S", {})
    assert "--model" not in b._argv_warm("analyze")
    b.close()


def test_idle_process_dies_when_its_parent_is_killed():
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    code = (
        "import time\n"
        "from glance.backends.proc import ProcRunner\n"
        "p = ProcRunner().spawn_idle(['sleep', '60'])\n"
        "print(p._proc.pid, flush=True)\n"
        "time.sleep(60)\n")
    src = str(Path(__file__).parent.parent / "src")
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONPATH": src})
    child = int(parent.stdout.readline())
    os.kill(child, 0)  # alive
    parent.kill()  # SIGKILL: no cleanup code runs
    parent.wait()
    deadline = time.monotonic() + 5
    gone = False
    while time.monotonic() < deadline and not gone:
        try:
            with open(f"/proc/{child}/stat") as f:
                gone = f.read().rsplit(")", 1)[1].split()[0] == "Z"  # dead, awaiting a reaper
        except FileNotFoundError:
            gone = True
        time.sleep(0.05)
    if not gone:
        os.kill(child, 9)
    assert gone, "idle process outlived its parent"


@pytest.mark.parametrize("value", ["--flag", "a b", "", "x;y"])
def test_config_rejects_bad_model_names(tmp_path, value):
    from glance.config import ConfigError

    p = tmp_path / "c.toml"
    p.write_text(f'[backend.models]\ncodex = "{value}"\n')
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "backend.models.codex" in str(e.value)


def test_real_claude_model_reaches_the_argv(log):
    b = make(log)
    b.set_model("sonnet")
    b.analyze("hello", CTX)
    argv = records(log)[0]["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_backendcfg_warm_wanted():
    assert BackendCfg(choice="claude").warm_wanted()
    assert not BackendCfg(choice="claude", warm=False).warm_wanted()
    assert not BackendCfg(choice="codex", warm=True).warm_wanted()
    assert not BackendCfg().warm_wanted()


def test_warm_key_parses(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[backend]\nwarm = false\n")
    assert load_config(p).backend.warm is False


