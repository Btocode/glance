"""The ctl messages behind `glance backend ...`, `glance rewrite` and `glance profile`."""
from types import SimpleNamespace

import pytest

from glance.audience.overrides import Overrides
from glance.backends.base import Health
from glance.backends.keystore import MemoryKeyStore
from glance.backends.supervisor import AiUnavailable
from glance.config import load_config
from glance.daemon.backend_ctl import BackendControl

READY = {"claude": Health(True, True, ""), "codex": Health(True, False, ""),
         "gemini": Health(True, None, ""), "opencode": Health(False, None, ""),
         "api": Health(True, False, "")}


class StubSupervisor:
    def __init__(self):
        self.healths_calls = []
        self.used = []
        self.tested = []
        self.test_result = (True, "ok")
        self.unavailable = None
        self.rewrites = []

    def healths(self, refresh=False):
        self.healths_calls.append(refresh)
        return READY

    def use(self, name):
        self.used.append(name)

    def test(self, name):
        self.tested.append(name)
        return self.test_result

    def rewrite(self, text, tone, ctx, custom=None, speculative=False):
        self.rewrites.append((text, tone))
        if self.unavailable:
            raise AiUnavailable(self.unavailable)
        yield "Sure, "
        yield "will do."


class StubAi:
    def __init__(self):
        self.r = SimpleNamespace(key="slack:eng", source="rule",
                                 profile=SimpleNamespace(name="team_channel"))
        self.overridden = []
        self.refreshed = 0

    def resolved(self):
        return self.r

    def set_profile_override(self, name):
        if name == "bogus":
            raise ValueError("unknown profile: bogus")
        self.overridden.append(name)

    def refresh_status(self):
        self.refreshed += 1


@pytest.fixture
def rig(tmp_path):
    sup, ai, keys = StubSupervisor(), StubAi(), MemoryKeyStore()
    overrides = Overrides(tmp_path / "overrides.json")
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("")
    calls = []

    def run_bg(work, done):  # synchronous, but the same shape as the daemon's
        calls.append(work)
        done(work())

    ctl = BackendControl(sup, ai, keys, lambda: load_config(cfg_path), overrides, run_bg,
                         cfg_path)
    out = []

    def send(msg_type, payload=None):
        out.clear()
        assert ctl.handles(msg_type)
        ctl.handle(msg_type, payload or {}, out.append)
        assert len(out) == 1
        return out[0]

    return SimpleNamespace(send=send, sup=sup, ai=ai, keys=keys, overrides=overrides,
                           cfg_path=cfg_path, ctl=ctl)


def test_handles_only_its_own_messages(rig):
    assert not rig.ctl.handles("status") and not rig.ctl.handles("pause")
    assert all(rig.ctl.handles(m) for m in (
        "backend_list", "backend_use", "backend_test", "backend_key", "backend_api",
        "rewrite", "profile_show", "profile_set", "profile_clear"))


def test_backend_list(rig):
    reply = rig.send("backend_list")
    assert reply["ok"] and reply["choice"] is None
    assert reply["backends"]["claude"] == {"installed": True, "signed_in": True, "detail": ""}
    assert reply["backends"]["gemini"]["signed_in"] is None
    assert rig.sup.healths_calls == [False]
    rig.send("backend_list", {"refresh": True})
    assert rig.sup.healths_calls == [False, True]


def test_backend_use_ready_and_unknown_are_accepted_and_written(rig):
    assert rig.send("backend_use", {"name": "claude"}) == {"ok": True, "name": "claude"}
    assert rig.sup.used == ["claude"]
    assert rig.send("backend_use", {"name": "gemini"})["ok"]  # sign-in not detectable
    assert rig.ai.refreshed == 2


def test_backend_use_refuses_a_backend_that_is_not_connected(rig):
    for name in ("codex", "opencode", "api"):
        reply = rig.send("backend_use", {"name": name})
        assert reply == {"ok": False, "error": "not_ready", "name": name}
    assert rig.sup.used == []
    assert rig.send("backend_use", {"name": "bard"})["error"] == "unknown backend"
    assert rig.send("backend_use", {})["ok"] is False


def test_backend_test_defaults_to_the_chosen_backend(rig):
    assert rig.send("backend_test", {"name": None})["error"] == "no backend chosen"
    rig.cfg_path.write_text('[backend]\nchoice = "claude"\n')
    assert rig.send("backend_test", {"name": None}) == {
        "ok": True, "name": "claude", "passed": True, "detail": "ok"}
    rig.sup.test_result = (False, "auth")
    reply = rig.send("backend_test", {"name": "gemini"})
    assert reply == {"ok": True, "name": "gemini", "passed": False, "detail": "auth"}
    assert rig.sup.tested == ["claude", "gemini"]
    assert rig.ai.refreshed == 2


def test_rewrite_joins_the_deltas_and_never_needs_a_focused_field(rig):
    rig.ai.r = None
    reply = rig.send("rewrite", {"tone": "friendlier", "text": "ok do it"})
    assert reply == {"ok": True, "text": "Sure, will do."}
    assert rig.sup.rewrites == [("ok do it", "friendlier")]


def test_rewrite_failures_carry_the_reason(rig):
    rig.sup.unavailable = "none"
    assert rig.send("rewrite", {"tone": "shorter", "text": "hi"}) == {"ok": False, "error": "none"}
    rig.sup.unavailable = "limit"
    assert rig.send("rewrite", {"tone": "shorter", "text": "hi"})["error"] == "limit"


def test_rewrite_validates_input(rig):
    assert rig.send("rewrite", {"tone": "sarcastic", "text": "hi"})["error"] == "unknown tone"
    assert rig.send("rewrite", {"tone": "shorter", "text": ""})["error"] == "no text"
    assert rig.send("rewrite", {"tone": "shorter", "text": 5})["error"] == "no text"
    assert rig.sup.rewrites == []


def test_backend_key_goes_to_the_keystore_only(rig):
    reply = rig.send("backend_key", {"provider": "openai", "key": "sk-secret-123"})
    assert reply == {"ok": True, "provider": "openai"}
    assert rig.keys.get("openai") == "sk-secret-123"
    assert "sk-secret-123" not in rig.cfg_path.read_text()
    assert "sk-secret-123" not in str(reply)
    assert rig.sup.healths_calls == [True]  # re-checked, so onboarding turns green
    rig.send("backend_key", {"key": "sk-ant-1"})  # default: the configured provider
    assert rig.keys.get("anthropic") == "sk-ant-1"


def test_backend_key_rejects_bad_input(rig):
    assert rig.send("backend_key", {"key": "   "})["ok"] is False
    assert rig.send("backend_key", {"provider": "bard", "key": "k"})["error"] == "unknown provider"
    assert rig.send("backend_key", {"key": "x" * 5000})["ok"] is False


def test_backend_api_writes_provider_model_and_base_url(rig):
    reply = rig.send("backend_api", {"provider": "openai", "model": "gpt-x",
                                     "base_url": "https://example.test/v1"})
    assert reply["ok"]
    cfg = load_config(rig.cfg_path).backend
    assert (cfg.api_provider, cfg.api_model, cfg.api_base_url) == (
        "openai", "gpt-x", "https://example.test/v1")
    assert rig.send("backend_api", {"provider": "bard"})["ok"] is False
    assert rig.send("backend_api", {"base_url": "ftp://x"})["ok"] is False


def test_profile_show_set_clear(rig):
    assert rig.send("profile_show") == {"ok": True, "profile": "team_channel",
                                        "key": "slack:eng", "source": "rule"}
    assert rig.send("profile_set", {"name": "dm"})["ok"]
    assert rig.ai.overridden == ["dm"] and rig.overrides.get("slack:eng") == "dm"
    assert rig.send("profile_clear")["ok"]
    assert rig.ai.overridden == ["dm", None] and rig.overrides.get("slack:eng") is None


def test_profile_errors(rig):
    assert rig.send("profile_set", {"name": "bogus"}) == {"ok": False,
                                                          "error": "unknown profile: bogus"}
    assert rig.overrides.get("slack:eng") is None
    rig.ai.r = None
    for m in ("profile_show", "profile_set", "profile_clear"):
        assert rig.send(m, {"name": "dm"})["error"] == "no focused field"


def test_rewrite_too_long_has_its_own_error(rig):
    reply = rig.send("rewrite", {"tone": "shorter", "text": "x" * 20_001})
    assert reply["ok"] is False and "too long" in reply["error"]


def test_profile_set_non_string_is_refused_not_raised(rig):
    assert rig.send("profile_set", {"name": 5}) == {"ok": False, "error": "unknown profile"}


def test_backend_keys_says_yes_or_no_and_never_the_key(rig):
    rig.keys.set("anthropic", "sk-ant-secret")
    reply = rig.send("backend_keys")
    assert reply == {"ok": True, "keys": {"anthropic": True, "openai": False}}
    assert "sk-ant-secret" not in repr(reply)


def test_backend_key_delete(rig):
    rig.keys.set("openai", "sk-x")
    assert rig.send("backend_key_delete", {"provider": "openai"}) == {
        "ok": True, "provider": "openai"}
    assert rig.keys.get("openai") is None
    assert rig.sup.healths_calls[-1] is True  # the api row turns signed out now
    assert rig.send("backend_key_delete", {"provider": "nope"})["ok"] is False
