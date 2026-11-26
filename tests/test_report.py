"""glance report: what a diagnostics file may and may not contain (nothing real is read)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from glance import cli, report

HOME = "/home/zelda-planted"
USER = "zelda-planted"
HOST = "zelda-laptop-7"
API_KEY = "sk-ant-api03-Qx7vT2mZp9LkR4sW8yNbC6dHjF3gE1aU5oIqXrYtVwZ0"
TYPED = "my secret meeting with Dr Plantedname about the merger"
TITLE = "Inbox (3) - planted.person@example.com - Mail"
URL = "https://intranet.example.com/planted/doc?id=42"
CLIP = "clipboard-planted-content-value"


def sources(**over):
    log = [
        "glanced_started tray=True",
        f"check_done ms=12 issues=3 wm_class=Thunderbird title=<redacted> app={USER}",
        "check_done ms=40 issues=0 backend=harper",
        "check_done ms=95 issues=1",
        f"rewrite_error kind=TimeoutError detail={URL}",
        "Traceback (most recent call last):",
        f'  File "{HOME}/.local/share/glance-src/src/glance/daemon/main.py", line 3',
        f"ValueError: {TYPED}",
        f"selection_action action=explain text={CLIP} len=57",
        f"backend_key key={API_KEY} ok=True",
        "glanced_forced_exit step=children ms=3001",
        "glance.service: Failed with result 'exit-code'.",
        f"proc_stderr line={TITLE}",
    ]
    base = dict(  # noqa: C408 - reads as the Sources fields
        versions={"glance": "0.4.0", "glance_core": "0.3.0", "harper-ls": "harper-ls 3.3.0"},
        system={"os": "Ubuntu 24.04", "session": "x11", "desktop": "ubuntu:GNOME",
                "python": "3.12.3"},
        doctor=[("harper", "fail", f"{HOME}/.local/share/glance/bin/harper-ls is missing"),
                ("backend", "ok", "claude signed in"),
                ("daemon", "ok", f"running on {HOST}")],
        backends={"claude": {"installed": True, "signed_in": True},
                  "api": {"installed": True, "signed_in": False}},
        choice="claude",
        features=["selection  on   Selection actions (selection.enabled)"],
        config={"api": {"anthropic_key": API_KEY, "provider": "anthropic"},
                "general": {"min_chars_chip": 12, "check_updates": True},
                "ui": {"custom_instruction": TYPED, "theme": "dark"},
                "privacy": {"app_denylist": ["keepassxc", f"{HOME}/bin/thing"]},
                "shot": {"save_dir": f"{HOME}/Pictures"}},
        log=log, home=HOME, user=USER, host=HOST)
    base.update(over)
    return report.Sources(**base)


PLANTED = (HOME, USER, HOST, API_KEY, TYPED, "Plantedname", TITLE, "planted.person", URL,
           "intranet.example.com", CLIP, "Thunderbird", "merger")


def test_report_contains_no_planted_secret_text_or_path():
    text = report.build(sources())
    for s in PLANTED:
        assert s not in text, s
    assert "/home/" not in text


def test_report_keeps_what_helps():
    text = report.build(sources())
    for want in ("glance: 0.4.0", "glance_core: 0.3.0", "session: x11", "[fail] harper:",
                 "~/.local/share/glance/bin/harper-ls is missing",
                 "claude: installed=yes signed_in=yes", "api: installed=yes signed_in=no", "in use: claude",
                 '"min_chars_chip": 12', '"anthropic_key": "<redacted>"', '"theme": "dark"',
                 '"custom_instruction": "<text>"', '"save_dir": "<path>"', '"keepassxc"',
                 "python_traceback: 1", "forced_exit: 1", "unit_failed: 1",
                 "check_done.ms: n=3 p50=40 p95=95 max=95", "check_done ms=12 issues=3",
                 "rewrite_error kind=TimeoutError", "selection_action action=explain len=57",
                 "Selection actions"):
        assert want in text, want


def test_log_filter_keeps_only_events_and_safe_fields():
    f = report.filter_log_line
    assert f("check_done ms=12 wm_class=firefox note=Hello") == "check_done ms=12"
    assert f("Traceback (most recent call last):") is None
    assert f("ValueError: something typed") is None
    assert f("  indented continuation") is None
    assert f("x") is None
    assert f("hello there typed words") is None
    assert f("notify a=1 free text") is None
    assert f("event mode=shortcut token=abc ok=False") == "event mode=shortcut ok=False"


def test_glanced_down_and_no_config():
    text = report.build(sources(backends=None, config=None, log=[]))
    assert "glanced is not running" in text and "defaults (no config file)" in text
    assert "no timing events" in text


def test_cli_report_writes_file_and_prints_issue_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "collect", lambda progress=None: sources())
    monkeypatch.setattr(report, "default_path", lambda today=None: tmp_path / "r.txt")
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "r.txt" in out and report.ISSUES_URL in out
    assert (tmp_path / "r.txt").read_text().startswith("Glance diagnostics report")


def test_cli_report_stdout(monkeypatch, capsys):
    monkeypatch.setattr(report, "collect", lambda progress=None: sources())
    assert cli.main(["report", "--stdout"]) == 0
    out = capsys.readouterr().out
    assert "== Doctor" in out and API_KEY not in out


def test_default_path_is_in_home():
    p = report.default_path(date(2026, 11, 26))
    assert p.name == "glance-report-2026-11-26.txt" and p.parent == Path.home()
