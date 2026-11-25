import json

import pytest

from glance.audience.overrides import Overrides
from glance.audience.profiles import BUILTIN, load_profiles
from glance.audience.resolver import DEFAULT_RULES, AudienceResolver, Rule, context_key
from glance.config import ConfigError
from glance.grammar.filters import IssueFilter
from glance.grammar.issues import Issue
from glance.grammar.store import IgnoredRules, PersonalDictionary
from glance.protocol import FieldSnapshot

SLACK_CHANNEL = "general (Channel) - Acme - Slack"
SLACK_DM = "Sam Lee (DM) - Acme - Slack"
DISCORD_CHANNEL = "#general | My Server - Discord"


def snap(wm_class="gedit", url=None, title="t"):
    return FieldSnapshot(
        app=wm_class, wm_class=wm_class, title=title, url=url, role="entry", text="hi",
        caret=2, caret_rect=None, prev_char_rect=None, field_rect=None, window_rect=None,
        field_id="f", capture_confidence=1.0, seq=1, pid=1, gated=None)


def resolver(overrides=None, rules=DEFAULT_RULES, profiles=None):
    return AudienceResolver(profiles or dict(BUILTIN), rules, overrides or {})


class DictOverrides:
    def __init__(self, d):
        self.d = d

    def get(self, key):
        return self.d.get(key)


@pytest.mark.parametrize("wm,url,title,expect", [
    ("google-chrome", "https://claude.ai/new", "t", "ai_prompt"),
    ("brave-browser-nightly", "https://chatgpt.com/c/1", "t", "ai_prompt"),
    ("google-chrome", "https://mail.google.com/mail/u/0/#inbox?compose=new", "t", "email"),
    ("firefox", "https://github.com/o/r/pull/12", "t", "code_review"),
    ("google-chrome", "https://x.com/home", "t", "public_post"),
    ("slack", None, SLACK_CHANNEL, "team_channel"),
    ("Slack", None, SLACK_DM, "dm"),
    ("discord", None, DISCORD_CHANNEL, "team_channel"),
    ("discord", "https://discord.com/channels/@me", "Friends - Discord", "dm"),
    ("google-chrome", "https://app.slack.com/client/T1/C1", "x", "team_channel"),
    ("google-chrome", "https://discord.com/channels/@me", "x", "dm"),
    ("gedit", None, "t", "default"),
])
def test_default_rules(wm, url, title, expect):
    r = resolver().resolve(snap(wm, url, title))
    assert r.profile.name == expect
    assert r.source == ("default" if expect == "default" else "rule")


def test_first_rule_wins():
    rules = (Rule("email", url="example"), Rule("dm", url="example\\.com"))
    assert resolver(rules=rules).resolve(snap(url="https://example.com/")).profile.name == "email"


def test_override_beats_rules_and_is_per_context():
    ov = DictOverrides({"claude.ai/new": "email"})
    res = resolver(ov)
    r = res.resolve(snap("google-chrome", "https://claude.ai/new"))
    assert (r.profile.name, r.source, r.key) == ("email", "override", "claude.ai/new")
    assert res.resolve(snap("slack", None, SLACK_CHANNEL)).profile.name == "team_channel"
    assert res.resolve(snap("google-chrome", "https://claude.ai/new")).source == "override"


def test_context_key():
    assert context_key(snap("firefox", "https://github.com/o/r/pull/12")) == "github.com/o"
    assert context_key(snap("Slack", None, SLACK_CHANNEL)) == "slack|general"
    assert context_key(snap("slack", None, SLACK_DM)) == "slack|Sam Lee"
    assert context_key(snap("discord", None, DISCORD_CHANNEL)) == "discord|general"
    assert context_key(snap("GEdit")) == "gedit"


def test_profile_table_overrides_builtin():
    ps = load_profiles({"profile": [{"name": "dm", "strictness": "strict"},
                                    {"name": "mine", "label": "Mine", "target_tone": "terse",
                                     "chips": ["shorter"], "strictness": "normal"}]})
    assert ps["dm"].strictness == "strict"
    assert ps["dm"].chips == BUILTIN["dm"].chips
    assert ps["mine"].chips == ("shorter",)
    assert ps["email"] == BUILTIN["email"]


def test_unknown_chip_key_is_config_error():
    with pytest.raises(ConfigError):
        load_profiles({"profile": [{"name": "dm", "chips": ["shorter", "bogus"]}]})


def test_ctx_has_no_title_or_url():
    s = snap("google-chrome", "https://claude.ai/new", "SECRET TITLE")
    res = resolver()
    ctx = res.ctx(s, res.resolve(s), mixed_language=True)
    assert ctx.app_label == "google-chrome"
    assert ctx.profile == "ai_prompt" and ctx.mixed_language is True
    blob = repr(ctx)
    assert "SECRET" not in blob and "claude.ai" not in blob


def test_dm_ignores_capitalization_rule(tmp_path):
    f = IssueFilter(PersonalDictionary(tmp_path / "d.txt"), IgnoredRules(tmp_path / "i.json"), [])
    t = "hello there"
    i = Issue("h:SentenceCapitalization:0:5", "harper", "SentenceCapitalization", "m",
              0, 5, ("Hello",), True)
    assert f.apply(t, [i], "slack", extra_ignored=frozenset(BUILTIN["default"].ignore_rules)) == [i]
    assert f.apply(t, [i], "slack", extra_ignored=frozenset(BUILTIN["dm"].ignore_rules)) == []


def test_overrides_file_holds_only_keys_and_profile_names(tmp_path):
    p = tmp_path / "sub" / "overrides.json"
    o = Overrides(p)
    assert o.get("claude.ai/new") is None
    o.set("claude.ai/new", "email")
    o.set("slack|general", "dm")
    assert json.loads(p.read_text()) == {"claude.ai/new": "email", "slack|general": "dm"}
    assert Overrides(p).get("slack|general") == "dm"
    o.clear("slack|general")
    assert json.loads(p.read_text()) == {"claude.ai/new": "email"}
    p.write_text("{corrupt")
    assert Overrides(p).get("x") is None


def test_dm_profile_silences_casual_harper_lints_live(tmp_path, monkeypatch):
    from glance.grammar.harper import HarperClient
    from glance.paths import harper_bin
    binary = harper_bin()
    if not binary.exists():
        pytest.skip("harper-ls not installed")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    c = HarperClient(binary, tmp_path / "dictionary.txt", timeout_s=3.0)
    c.start()
    try:
        f = IssueFilter(PersonalDictionary(tmp_path / "d.txt"),
                        IgnoredRules(tmp_path / "i.json"), [])
        extra = frozenset(BUILTIN["dm"].ignore_rules)
        for t in ("i think so", "hey how are you", "hello there. how are you"):
            issues = c.check(t)
            assert f.apply(t, issues, "slack", extra) == [], t
        assert c.check("i think so")  # the default profile would still show it
    finally:
        c.stop()
