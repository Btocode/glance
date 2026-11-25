"""Global constraint: typed text never in argv. The draft and the "Rewrite as…" instruction are
both typed by the user; every CLI backend must carry them on stdin, the API backend in the body."""
import json
from pathlib import Path

import httpx
import pytest

from glance.backends.api import ApiBackend
from glance.backends.base import Ctx
from glance.backends.claude import ClaudeBackend
from glance.backends.codex import CodexBackend
from glance.backends.gemini import GeminiBackend
from glance.backends.keystore import MemoryKeyStore
from glance.backends.opencode import OpencodeBackend
from glance.backends.proc import ProcRunner

FAKE = Path(__file__).parent / "fake_cli"
CTX = Ctx("dm", "casual", "relaxed", "Slack")
TEXT_MARK = "QZXTEXTMARK"
INSTR_MARK = "QZXINSTRMARK"


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


def _make(name, log, mode):
    env = {"FAKE_LOG": str(log), "FAKE_MODE": mode, "FAKE_AUTH": "in", "FAKE_CREDS": "1"}
    cls = {"claude": ClaudeBackend, "codex": CodexBackend, "gemini": GeminiBackend,
           "opencode": OpencodeBackend}[name]
    return cls(ProcRunner(env=env), binary=str(FAKE / name))


@pytest.mark.parametrize("name", ["claude", "codex", "gemini", "opencode"])
def test_cli_argv_never_carries_the_text_or_the_custom_instruction(name, tmp_path):
    log = tmp_path / "fake.log"
    rw, an = _make(name, log, "rewrite"), _make(name, log, "analyze")
    try:
        list(rw.rewrite(f"hello {TEXT_MARK} there", "custom", CTX,
                        f"sound like a {INSTR_MARK} pirate"))
        an.analyze(f"hello {TEXT_MARK} again", CTX)
    finally:
        for b in (rw, an):
            getattr(b, "stop", lambda: None)()
    recs = [json.loads(x) for x in log.read_text().splitlines()]
    assert recs
    for r in recs:
        argv = " ".join(r["argv"])
        assert TEXT_MARK not in argv and INSTR_MARK not in argv
    stdins = [r["stdin"] for r in recs if r.get("stdin")]
    assert any(f"sound like a {INSTR_MARK} pirate" in s and f"hello {TEXT_MARK} there" in s
               and "<glance_instruction>" in s for s in stdins)
    assert any(f"hello {TEXT_MARK} again" in s and "glance_instruction" not in s for s in stdins)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_api_carries_the_custom_instruction_in_the_user_message_only(provider):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, content=b"")

    keys = MemoryKeyStore()
    keys.set(provider, "sk-test")
    be = ApiBackend(keys, provider=provider, model="m-test" if provider == "openai" else "",
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    list(be.rewrite(f"hello {TEXT_MARK}", "custom", CTX, f"be a {INSTR_MARK}"))
    assert TEXT_MARK not in seen["url"] and INSTR_MARK not in seen["url"]
    msgs = seen["body"]["messages"]
    system = seen["body"].get("system") or next(m["content"] for m in msgs if m["role"] == "system")
    user = next(m["content"] for m in msgs if m["role"] == "user")
    assert INSTR_MARK not in system and TEXT_MARK not in system
    assert f"be a {INSTR_MARK}" in user and f"hello {TEXT_MARK}" in user
