"""Images to an AI: the payloads (Anthropic, OpenAI-compatible, the claude CLI; no network), the supervisor sending an image to one named backend only, the one-time notice store and Translate's target language."""
from __future__ import annotations

import dataclasses
import json
from datetime import date

import httpx
import pytest

from glance.backends.base import BackendError, BaseBackend
from glance.backends.budget import Budget
from glance.backends.supervisor import AiUnavailable, BackendSupervisor
from glance.backends.usage import UsageLedger
from glance.config import BackendCfg, Config, SelectionCfg
from glance.shot.consent import ALLOW, ASK, REFUSE, ImageConsent
from glance.shot.prompts import translate_target
from tests.fakes import FakeClock
from tests.shot_fakes import VisionFake

# ------------------------------------------------------------------ consent


def test_consent_once_per_backend(tmp_path):
    c = ImageConsent(tmp_path / "state" / "shot-images.json")
    assert c.decide("ask", "claude") == ASK
    assert c.decide("allow", "claude") == ALLOW
    assert c.decide("never", "claude") == REFUSE
    c.always_allow("claude")
    assert c.decide("ask", "claude") == ALLOW and c.decide("ask", "api:x:y") == ASK
    assert c.decide("never", "claude") == REFUSE  # never wins over a stored allow
    data = json.loads((tmp_path / "state" / "shot-images.json").read_text())
    assert data == {"allowed": ["claude"]}
    assert (tmp_path / "state" / "shot-images.json").stat().st_mode & 0o777 == 0o600
    (tmp_path / "state" / "shot-images.json").write_text("{broken")
    assert c.decide("ask", "claude") == ASK


def test_translate_target():
    assert translate_target(Config()) == "English"
    cfg = dataclasses.replace(Config(), selection=SelectionCfg(translate_to="Bangla"))
    assert translate_target(cfg) == "Bangla"  # the one [selection] translate_to


# ------------------------------------------------------------------ backends: image payloads


def test_api_anthropic_image_payload():
    from glance.backends.api import ApiBackend
    from glance.backends.keystore import MemoryKeyStore

    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        ev = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "An error"}}
        return httpx.Response(200, content=b"data: " + json.dumps(ev).encode() + b"\n\n")

    keys = MemoryKeyStore()
    keys.set("anthropic", "sk")
    api = ApiBackend(keys, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "".join(api.see(b"\x89PNGdata", "SYS", "What is this?")) == "An error"
    b = seen["body"]
    assert b["system"] == "SYS" and b["stream"] is True
    content = b["messages"][0]["content"]
    assert content[0] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "iVBOR2RhdGE="}}
    assert content[1] == {"type": "text", "text": "What is this?"}
    assert api.vision_key() == "api:anthropic:api.anthropic.com"
    assert "Anthropic" in api.vision_label()


def test_api_openai_image_payload():
    from glance.backends.api import ApiBackend
    from glance.backends.keystore import MemoryKeyStore

    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        ev = {"choices": [{"delta": {"content": "ok"}}]}
        return httpx.Response(200, content=b"data: " + json.dumps(ev).encode() + b"\n\n")

    keys = MemoryKeyStore()
    keys.set("openai", "sk")
    api = ApiBackend(keys, provider="openai", model="gpt-x", base_url="http://localhost:1234/v1",
                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert "".join(api.see(b"PNG", "SYS", "Q")) == "ok"
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["content"] == [{"type": "text", "text": "Q"}, {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,UE5H"}}]
    assert api.vision_key() == "api:openai:localhost"


def test_claude_image_goes_on_stdin_as_stream_json():
    from glance.backends.claude import ClaudeBackend

    class Runner:
        def stream(self, argv, stdin, timeout_s):
            self.argv, self.stdin = argv, stdin
            yield json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "delta": {"type": "text_delta", "text": "It"}}})
            yield json.dumps({"type": "result", "result": "It", "total_cost_usd": 0.01,
                              "usage": {"input_tokens": 900, "output_tokens": 3}})

    r = Runner()
    c = ClaudeBackend(r)
    assert c.vision and "".join(c.see(b"PNG", "SYS", "Explain")) == "It"
    a = r.argv
    assert a[a.index("--input-format") + 1] == "stream-json"
    assert a[a.index("--output-format") + 1] == "stream-json"
    assert a[a.index("--system-prompt") + 1] == "SYS" and a[a.index("--tools") + 1] == ""
    assert not any("PNG" in x or x.endswith(".png") for x in a)  # never a file, never argv
    msg = json.loads(r.stdin)
    assert msg["type"] == "user" and msg["message"]["role"] == "user"
    assert msg["message"]["content"][0]["source"] == {
        "type": "base64", "media_type": "image/png", "data": "UE5H"}
    assert msg["message"]["content"][1] == {"type": "text", "text": "Explain"}
    assert c.take_usage().cost_usd == 0.01


def test_other_backends_take_no_images():
    from glance.backends.codex import CodexBackend

    assert BaseBackend.vision is False and getattr(CodexBackend, "vision", False) is False
    with pytest.raises(BackendError) as e:
        list(BaseBackend().see(b"x", "s", "u"))
    assert e.value.detail == "no_vision"


def make_sup(tmp_path, backends, choice, enabled=(), limit=None):
    clock = FakeClock()
    cfg = BackendCfg(choice=choice, enabled=tuple(enabled))
    budget = Budget(clock, lambda: cfg, tmp_path / "usage.json", lambda: date(2026, 11, 26))
    ledger = UsageLedger(tmp_path / "ledger.json", today=lambda: date(2026, 11, 26))
    return BackendSupervisor(backends, lambda: cfg, budget, clock, on_battery=lambda: False,
                             on_status=lambda _s: None, ledger=ledger,
                             cost_limit=lambda: limit), ledger


def test_supervisor_sends_an_image_only_to_the_named_backend(tmp_path):
    text_only, seer = VisionFake("codex", False), VisionFake("api", True)
    sup, ledger = make_sup(tmp_path, {"codex": text_only, "api": seer}, "codex", ["api"])
    name, backend = sup.vision_backend()
    assert (name, backend) == ("api", seer)
    assert "".join(sup.see("api", b"IMG", "s", "u")) == "seen"
    assert seer.seen == [b"IMG"]
    with pytest.raises(AiUnavailable):
        list(sup.see("claude", b"IMG", "s", "u"))  # not connected: no fallback
    report = ledger.periods()["today"]
    assert report["total"]["calls"] >= 1


def test_supervisor_vision_respects_the_cost_limit(tmp_path):
    seer = VisionFake("api", True)
    sup, _ledger = make_sup(tmp_path, {"api": seer}, "api", limit=0.0)
    with pytest.raises(AiUnavailable) as e:
        list(sup.see("api", b"IMG", "s", "u"))
    assert e.value.reason == "limit" and seer.seen == []
