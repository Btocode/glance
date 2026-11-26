from dataclasses import replace
from pathlib import Path

import pytest

from glance.config import load_config
from glance.privacy.gate import FieldMeta, GateDecision, PrivacyGate

META = FieldMeta(app="gedit", wm_class="gedit", title="doc", url=None, role="text",
                 states=frozenset({"editable", "focused"}), pid=1, text_len=40)

ALLOWED = GateDecision(True, None)


def gate() -> PrivacyGate:
    return PrivacyGate(load_config(Path("/nonexistent")))


def reason(**change) -> str | None:
    return gate().evaluate(replace(META, **change)).reason


@pytest.mark.parametrize("change, reason", [
    ({"role": "password text"}, "password"),
    ({"wm_class": "org.remmina.Remmina"}, "app_denylist"),
    ({"app": "Seahorse"}, "app_denylist"),
    ({"wm_class": "AnyDesk"}, "app_denylist"),
    ({"wm_class": "google-chrome", "title": "New Tab - Google Chrome (Incognito)",
      "url": "https://x.test/"}, "incognito"),
    ({"wm_class": "firefox", "title": "Mozilla Firefox Private Browsing",
      "url": "https://x.test/"}, "incognito"),
    ({"wm_class": "google-chrome", "url": "https://accounts.google.com/v3/signin"},
     "url_denylist"),
    ({"wm_class": "google-chrome", "url": "https://mybank.example/transfer"}, "url_denylist"),
    ({"wm_class": "google-chrome", "url": None}, "url_unknown"),
    ({"wm_class": "com.mitchellh.ghostty"}, "terminal"),
    ({"wm_class": "gnome-terminal-server", "terminal_fg": "zsh"}, "terminal"),
    ({"wm_class": "org.gnome.Ptyxis", "terminal_fg": "zsh"}, "terminal"),
    ({"wm_class": "org.gnome.Terminal", "terminal_fg": "zsh"}, "terminal"),
    ({"text_len": 20001}, "too_long"),
    ({"states": frozenset({"focused"})}, "not_editable"),
])
def test_gate_blocks(change, reason):
    assert gate().evaluate(replace(META, **change)) == GateDecision(False, reason)


def test_gate_allows_plain_field_and_slack_without_url():
    g = gate()
    assert g.evaluate(META).allowed and g.evaluate(replace(META, wm_class="Slack")).allowed
    assert g.evaluate(META) == ALLOWED


def test_password_wins_over_everything():
    m = replace(META, role="password text", wm_class="AnyDesk")
    assert gate().evaluate(m).reason == "password"


def test_app_disabled_by_config(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[apps.gedit]\ncapture = false\n")
    g = PrivacyGate(load_config(p))
    assert g.evaluate(META) == GateDecision(False, "app_disabled")
    assert g.evaluate(replace(META, wm_class="Gedit")) == GateDecision(False, "app_disabled")
    assert g.evaluate(replace(META, wm_class="Slack")) == ALLOWED


# --- password -------------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"states": frozenset({"editable", "protected"})},
    {"role": "Password Text"},
    {"role": " password text "},
    {"role": "password"},
])
def test_password_variants(change):
    assert reason(**change) == "password"


# --- order: each reason beats every later one -----------------------------------------------

# One change per reason, composable: applying all changes from index i onward must yield
# reason i. Chrome is also made a terminal class so a single window can trip every check.
ORDER_CFG = """
[privacy]
terminal_wm_classes = ["google-chrome"]
[apps.google-chrome]
capture = false
"""
ORDERED = [
    ("password", {"role": "password text"}),
    ("app_denylist", {"app": "Seahorse"}),
    ("incognito", {"title": "x - Google Chrome (Incognito)"}),
    ("url_denylist", {"url": "https://mybank.example/"}),
    ("url_unknown", {"url": None}),
    ("terminal", {}),
    ("app_disabled", {}),
    ("too_long", {"text_len": 20001}),
    ("not_editable", {"states": frozenset({"focused"})}),
]


@pytest.mark.parametrize("i", range(len(ORDERED)))
def test_reasons_are_checked_in_documented_order(tmp_path, i):
    p = tmp_path / "c.toml"
    # terminal / app_disabled are switched on by config only from their own index onward
    cfg = ORDER_CFG if i <= 6 else ""
    if i == 6:
        cfg = "[apps.google-chrome]\ncapture = false\n"
    p.write_text(cfg)
    meta = replace(META, wm_class="google-chrome", url="https://x.test/")
    changes: dict = {}
    for name, change in ORDERED[i:]:
        if name == "url_unknown" and "url" in changes:
            continue  # url_denylist needs a URL; url_unknown is covered from its own index
        changes.update(change)
    assert PrivacyGate(load_config(p)).evaluate(replace(meta, **changes)).reason == ORDERED[i][0]


# --- app denylist ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", [
    {"wm_class": "KeePassXC"},
    {"app": "org.keepassxc.KeePassXC"},
    {"wm_class": "VirtualBox Machine"},
    {"wm_class": "virt-manager"},
    {"app": "Bitwarden"},
])
def test_app_denylist_matches_substring_case_insensitive(change):
    assert reason(**change) == "app_denylist"


# --- incognito ------------------------------------------------------------------------------

@pytest.mark.parametrize("wm_class, title", [
    ("google-chrome", "glance fixture y=0 - Google Chrome (Incognito)"),
    ("brave-browser-nightly", "glance fixture y=0 - Brave Canary (Private)"),
    ("firefox_firefox", "glance fixture y=0 — Mozilla Firefox Private Browsing"),
    ("microsoft-edge", "New tab - [InPrivate] - Microsoft Edge"),
    ("google-chrome", "x - google chrome (incognito)"),
])
def test_private_window_titles_from_findings(wm_class, title):
    assert reason(wm_class=wm_class, title=title, url="https://x.test/") == "incognito"


def test_normal_browser_titles_are_not_incognito():
    for wm_class, title in [("google-chrome", "glance fixture y=0 - Google Chrome"),
                            ("brave-browser-nightly", "glance fixture y=0 - Brave Canary"),
                            ("firefox", "glance fixture y=0 — Mozilla Firefox")]:
        d = gate().evaluate(replace(META, wm_class=wm_class, title=title, url="https://x.test/"))
        assert d == ALLOWED


# --- URL denylist ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://accounts.google.com/",
    "https://ACCOUNTS.GOOGLE.COM/",
    "https://accounts.google.com/v3/signin/identifier?continue=https://mail.google.com/#inbox",
    "https://login.microsoftonline.com/common/oauth2/authorize",
    "https://dev-1234.okta.com/app",
    "https://tenant.eu.auth0.com/u/x",
    "https://www.paypal.com/myaccount",
    "https://paypal.com/",
    "https://www.chase-bank.example/",
    "https://x.test/login",
    "https://x.test/login/",
    "https://x.test/login?next=/",
    "https://x.test/login#top",
    "https://x.test/account/sign-in",
    "https://x.test/sso/saml",
    "https://x.test/oauth/authorize?client_id=1",
    "https://shop.test/checkout",
    "https://x.test/Password/reset",
    "https://user:pw@accounts.google.com:443/x",
])
def test_url_denylist_blocks(url):
    assert reason(wm_class="google-chrome", url=url) == "url_denylist"


@pytest.mark.parametrize("url", [
    "https://claude.ai/new",
    "https://chatgpt.com/",
    "https://mail.google.com/mail/u/0/",
    "https://x.test/blog/loginless",
    "https://notpaypal.com.example/",
    "file:///home/u/fixture.html",
])
def test_url_denylist_allows_ordinary_pages(url):
    assert gate().evaluate(replace(META, wm_class="google-chrome", url=url)) == ALLOWED


def test_url_denylist_applies_outside_browsers():
    assert reason(wm_class="Slack", url="https://x.test/login") == "url_denylist"


def test_invalid_user_url_pattern_is_matched_literally_not_dropped(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[privacy]\nurl_denylist = ["secret(", "intranet"]\n')
    g = PrivacyGate(load_config(p))
    blocked = GateDecision(False, "url_denylist")
    assert g.evaluate(replace(META, wm_class="firefox", url="https://x.test/secret(1")) == blocked
    assert g.evaluate(replace(META, wm_class="firefox", url="https://intranet.test/")) == blocked
    assert g.evaluate(replace(META, wm_class="firefox", url="https://x.test/")) == ALLOWED


# --- URL unknown ----------------------------------------------------------------------------

@pytest.mark.parametrize("wm_class", [
    "google-chrome", "Google-chrome", "brave-browser", "brave-browser-nightly", "firefox",
    "firefox_firefox", "chromium", "chromium-browser",
])
@pytest.mark.parametrize("url", [None, "", "   "])
def test_browser_without_url_is_blocked(wm_class, url):
    assert reason(wm_class=wm_class, url=url) == "url_unknown"


# --- terminals ------------------------------------------------------------------------------

@pytest.mark.parametrize("wm_class", [
    "com.mitchellh.ghostty", "ghostty", "kitty", "Gnome-terminal-server", "xterm", "UXTerm",
    "Alacritty", "org.wezfurlong.wezterm", "konsole", "tilix",
])
@pytest.mark.parametrize("fg", [None, "", "zsh", "claude"])
def test_terminals_always_blocked_with_default_empty_allowlist(wm_class, fg):
    assert reason(wm_class=wm_class, terminal_fg=fg) == "terminal"


def test_terminal_allowlist_admits_only_listed_foreground(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[privacy]\nterminal_allowlist = ["claude"]\n')
    g = PrivacyGate(load_config(p))
    m = replace(META, wm_class="kitty")
    assert g.evaluate(replace(m, terminal_fg="claude")) == ALLOWED
    for fg in (None, "", "zsh", "Claude", "claude2"):
        assert g.evaluate(replace(m, terminal_fg=fg)) == GateDecision(False, "terminal")


# --- length / editable ----------------------------------------------------------------------

def test_length_boundary():
    assert gate().evaluate(replace(META, text_len=20000)) == ALLOWED
    assert reason(text_len=20001) == "too_long"


def test_read_only_field_blocked():
    assert reason(states=frozenset()) == "not_editable"
