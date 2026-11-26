import json

import httpx
import pytest

from glance.backends.api import ApiBackend
from glance.backends.base import BackendError, Ctx
from glance.backends.keystore import MemoryKeyStore
from glance.backends.prompts import TEST_SENTENCE

CTX = Ctx(profile="friend", target_tone="casual", strictness="normal", app_label="Chat")
ANALYSIS = {
    "tone": "casual", "mismatch": False, "reason": None, "flags": [],
    "issues": [{"quote": "Teh", "occurrence": 1, "message": "typo", "replacement": "The",
                "kind": "grammar"}],
}


def make(handler, provider="anthropic", model="", key="sk-test", base_url=""):
    keys = MemoryKeyStore()
    if key:
        keys.set(provider, key)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ApiBackend(keys, provider=provider, model=model, base_url=base_url, client=client)


def sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


def test_anthropic_analyze_uses_forced_tool():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "ignored"},
            {"type": "tool_use", "name": "report", "input": ANALYSIS}]})

    a = make(handler).analyze(TEST_SENTENCE, CTX)
    assert a.tone == "casual" and a.issues[0].quote == "Teh"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    b = seen["body"]
    assert b["tool_choice"] == {"type": "tool", "name": "report"}
    assert b["tools"][0]["name"] == "report" and "properties" in b["tools"][0]["input_schema"]
    assert b["model"] == "claude-haiku-5-5" and b["max_tokens"] == 4096
    assert "stream" not in b or b["stream"] is False


def test_anthropic_stream_yields_text_deltas():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        ev = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
              for t in ("Th", "an", "ks")]
        return httpx.Response(200, content=sse({"type": "message_start"}, *ev,
                                               {"type": "message_stop"}))

    out = list(make(handler).rewrite("thx", "friendlier", CTX))
    assert out == ["Th", "an", "ks"]
    assert seen["body"]["stream"] is True


def test_openai_json_schema_then_fallback_on_400():
    bodies = []

    def handler(req):
        assert req.url == httpx.URL("http://localhost:1234/v1/chat/completions")
        assert req.headers["authorization"] == "Bearer sk-o"
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": "unsupported response_format"})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANALYSIS)}}]})

    be = make(handler, provider="openai", model="m1", key="sk-o", base_url="http://localhost:1234/v1")
    assert be.analyze(TEST_SENTENCE, CTX).tone == "casual"
    assert len(bodies) == 2
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert bodies[0]["response_format"]["json_schema"]["strict"] is True
    assert bodies[1]["response_format"] == {"type": "json_object"}
    assert bodies[0]["model"] == "m1"


def test_openai_stream():
    def handler(req):
        body = json.loads(req.content)
        assert body["stream"] is True
        chunks = [{"choices": [{"delta": {"content": t}}]} for t in ("Hi", " there")]
        chunks.insert(0, {"choices": [{"delta": {"role": "assistant"}}]})
        return httpx.Response(200, content=sse(*chunks) + b"data: [DONE]\n\n")

    be = make(handler, provider="openai", model="m1", key="k")
    assert list(be.rewrite("hello", "shorter", CTX)) == ["Hi", " there"]


@pytest.mark.parametrize("status,kind", [(401, "auth"), (403, "auth"), (429, "rate_limit"),
                                         (503, "unavailable"), (500, "unavailable")])
def test_status_mapping(status, kind):
    be = make(lambda req: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(BackendError) as e:
        be.analyze("hi", CTX)
    assert e.value.kind == kind
    with pytest.raises(BackendError) as e:
        list(be.rewrite("hi", "shorter", CTX))
    assert e.value.kind == kind


def test_connect_error_is_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(BackendError) as e:
        make(handler).analyze("hi", CTX)
    assert e.value.kind == "unavailable"


def test_timeout_maps():
    def handler(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(BackendError) as e:
        make(handler).analyze("hi", CTX)
    assert e.value.kind == "timeout"
    with pytest.raises(BackendError) as e:
        list(make(handler).rewrite("hi", "shorter", CTX))
    assert e.value.kind == "timeout"


def test_cancel_closes_stream():
    def handler(req):
        ev = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}}
              for t in ("a", "b", "c")]
        return httpx.Response(200, content=sse(*ev))

    be = make(handler)
    it = be.rewrite("hi", "shorter", CTX)
    assert next(it) == "a"
    be.cancel()
    with pytest.raises(BackendError) as e:
        list(it)
    assert e.value.kind == "cancelled"


def test_no_key_means_not_signed_in():
    called = []
    be = make(lambda req: called.append(1) or httpx.Response(200), key="")
    h = be.health()
    assert h.installed is True and h.signed_in is False
    with pytest.raises(BackendError) as e:
        be.analyze("hi", CTX)
    assert e.value.kind == "auth" and not called


def test_health_signed_in_with_key():
    assert make(lambda r: httpx.Response(200)).health().signed_in is True


def test_openai_requires_model():
    be = make(lambda r: httpx.Response(200), provider="openai", model="", key="k")
    h = be.health()
    assert "set backend.api_model" in h.detail
    with pytest.raises(BackendError) as e:
        be.analyze("hi", CTX)
    assert e.value.kind == "unavailable" and "backend.api_model" in e.value.detail


def test_key_and_body_never_logged(capsys, monkeypatch):
    logged = []
    monkeypatch.setattr("glance.backends.api.log_event", lambda ev, **f: logged.append((ev, f)),
                        raising=False)
    be = make(lambda req: httpx.Response(401, json={"error": "bad key sk-test"}))
    with pytest.raises(BackendError) as e:
        be.analyze("secret draft text", CTX)
    blob = repr(logged) + str(e.value) + capsys.readouterr().out
    assert "sk-test" not in blob and "secret draft text" not in blob


def test_configure_switches_provider_model_and_url_live():
    from glance.backends.keystore import MemoryKeyStore

    keys = MemoryKeyStore()
    keys.set("openai", "sk-o")
    b = ApiBackend(keys, "anthropic")
    assert b.health().signed_in is False  # no anthropic key
    assert b.configure("openai", "gpt-x", "https://llm.example/v1/") is True
    assert b.health().signed_in is True
    assert b._prepare()[0] == "https://llm.example/v1/chat/completions"
    assert b.configure("openai", "gpt-x", "https://llm.example/v1") is False
    with pytest.raises(ValueError):
        b.configure("nope")
