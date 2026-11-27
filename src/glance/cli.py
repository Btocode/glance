"""`glance` command line.

    glance start                start Glance again (after Quit in the tray)
    glance status [--json]      what glanced is doing (never any text)
    glance pause [DURATION]     30m, 2h, 45s; a bare number is minutes; none: until resumed
    glance resume
    glance wrapper install|uninstall|status
    glance doctor [--fix|--undo] [--app ID]... [--skip ID]... [--json]
    glance usage [--days N] [--json]   AI calls, tokens and cost (never any text)
    glance backend list|use NAME|test [NAME]|set-key [--provider P]|model NAME [MODEL|--default]
    glance rewrite --tone TONE      stdin -> stdout, through the chosen AI
    glance profile [show|set NAME|clear]   the focused field's audience
    glance update [--check] [--main]   install the latest release (--main: the main branch);
                                --check only compares versions
    glance setup [--undo]       per-user setup after a package install (AUR, .deb, Homebrew)
    glance report [--stdout]    a diagnostics file to attach to an issue (no text, no secrets)
    glance --version
    glance features [NAME [on|off]]   list the features, or switch one (applies live)
    glance settings [SECTION]   open the Settings window
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from glance.integrations import claude_wrapper

EXIT_NOT_RUNNING = 3
EXIT_NO_AI = 4
SLOW_REQUEST_S = 60.0  # a real backend call: 8 s analyze / 20 s rewrite, plus a retry
_DURATION = re.compile(r"([0-9]+)([smh]?)")
BACKENDS = ("claude", "codex", "gemini", "opencode", "api")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "": 60}
# settings_model.SECTIONS' keys (a test keeps them equal; not imported: `glance` starts fast)
SETTINGS_SECTIONS = ("general", "features", "ai", "shortcuts", "apps", "privacy", "about")


def parse_duration(s: str) -> int:
    """Seconds in `30m`, `2h`, `45s` or a bare number of minutes; ValueError otherwise."""
    m = _DURATION.fullmatch(s)
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)] if m else 0
    if seconds <= 0:
        raise ValueError(f"not a duration: {s!r} (use 30m, 2h, 45s or a number of minutes)")
    return seconds


def _request(msg_type: str, payload: dict[str, Any],
             timeout_s: float = 3.0) -> dict[str, Any] | None:
    """glanced's reply to one control message; None when it is not running."""
    from glance.ipc import JsonLineClient
    from glance.paths import ctl_sock

    client = JsonLineClient(ctl_sock())
    try:
        client.connect(timeout_s=0)
        return client.request(msg_type, payload, timeout_s=timeout_s)
    except (ConnectionError, TimeoutError):
        return None
    finally:
        client.close()


def _progress(text: str) -> None:
    """What a command is waiting for, before a wait that can take seconds (stderr, so
    output meant for a pipe stays clean)."""
    print(text, file=sys.stderr, flush=True)


def _lines(value: dict[str, Any], prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, v in value.items():
        if isinstance(v, dict):
            out += _lines(v, f"{prefix}{key}.")
        else:
            shown = "none" if v is None else str(v).lower() if isinstance(v, bool) else v
            out.append(f"{prefix}{key}: {shown}")
    return out


def _control(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {}
    if args.cmd == "pause":
        payload["seconds"] = None if args.duration is None else parse_duration(args.duration)
    reply = _request(args.cmd, payload)
    if reply is None:
        print("glanced is not running", file=sys.stderr)
        return EXIT_NOT_RUNNING
    if not reply.get("ok"):
        print(f"glance: {reply.get('error', 'request refused')}", file=sys.stderr)
        return 1
    if args.cmd == "status":
        status = {k: v for k, v in reply.items() if k not in ("id", "ok")}
        print(json.dumps(status) if args.json else "\n".join(_lines(status)))
    return 0


def _shot() -> int:
    """`glance shot`: glanced opens the region picker (nothing is captured by this process)."""
    reply, code = _ask("shot", {})
    if reply is None:
        return code
    if code:
        print(f"glance: {reply.get('error', 'Glance Shot is off')}", file=sys.stderr)
        return 1
    return 0


_NO_AI = "Connect an AI: run `glance backend list` to see how, then `glance backend use <name>`."
_TONE_CHOICES = ("friendlier", "more_direct", "shorter", "professional", "sharpen_prompt",
                 "add_structure")


def _ask(msg_type: str, payload: dict[str, Any],
         timeout_s: float = 3.0) -> tuple[dict[str, Any] | None, int]:
    """(reply, 0) on success, else (None, exit code) with the message already printed."""
    reply = _request(msg_type, payload, timeout_s)
    if reply is None:
        print("glanced is not running", file=sys.stderr)
        return None, EXIT_NOT_RUNNING
    if not reply.get("ok"):
        return reply, 1
    return reply, 0


def _refused(reply: dict[str, Any]) -> int:
    print(f"glance: {reply.get('error', 'request refused')}", file=sys.stderr)
    return 1


def _backend_list() -> int:
    from glance.backends.base import Health
    from glance.backends.cli_path import NOT_FOUND
    from glance.ui.onboarding_model import build_rows, model_text

    _progress("Asking each AI command-line tool whether it is signed in...")
    reply, code = _ask("backend_list", {"refresh": True}, SLOW_REQUEST_S)
    if reply is None or code:
        return code if reply is None else _refused(reply)
    healths = {n: Health(bool(h.get("installed")), h.get("signed_in"), str(h.get("detail", "")))
               for n, h in reply.get("backends", {}).items()}
    rows = build_rows(healths, reply.get("choice"), reply.get("models") or {})
    if not any(r.state == "ready" for r in rows):
        print("No AI connected — run one of the commands below, then: "
              "glance backend use <name>")
    where = reply.get("where") or {}
    for r in rows:
        mark = "  (in use)" if r.selected and r.state == "ready" else ""
        print(f"{r.name:<9} {r.state_text.lower():<16} {r.command}{mark}")
        print(f"{'':<9} model: {model_text(r.model, r.name)}")
        if r.name in where:  # where glanced (not this shell) finds the CLI
            print(f"{'':<9} {'found at ' + where[r.name] if where[r.name] else NOT_FOUND}")
    return 0


def _backend_model(name: str, model: str | None, default: bool) -> int:
    if default == (model is not None):
        print("glance: give a model name, or --default", file=sys.stderr)
        return 2
    reply, code = _ask("backend_model", {"name": name, "model": None if default else model})
    if reply is None:
        return code
    if code:
        return _refused(reply)
    from glance.ui.onboarding_model import model_text

    print(f"{name} model: {model_text(str(reply.get('model') or ''), name)}"
          + ("  (default)" if reply.get("default") else ""))
    return 0


def _backend_use(name: str) -> int:
    from glance.backends.base import CONNECT

    reply, code = _ask("backend_use", {"name": name})
    if reply is None:
        return code
    if code == 0:
        print(f"using {name}")
        return 0
    if reply.get("error") == "not_ready":
        print(f"glance: {name} is not connected. Run: {CONNECT[name]}", file=sys.stderr)
        return 2
    return _refused(reply)


def _backend_test(name: str | None) -> int:
    from glance.backends.base import SIGN_IN

    _progress(f"Sending one real test request to {name or 'the AI in use'}...")
    reply, code = _ask("backend_test", {"name": name}, SLOW_REQUEST_S)
    if reply is None:
        return code
    if code:
        return _refused(reply)
    shown = reply.get("name", name)
    if reply.get("passed"):
        print(f"ok: {shown} answered a real test call")
        return 0
    detail = reply.get("detail", "")
    hint = f" To sign in, {SIGN_IN[shown]}." if detail == "auth" and shown in SIGN_IN else ""
    print(f"failed: {shown}: {detail}.{hint}", file=sys.stderr)
    return 1


def _backend_set_key(provider: str | None) -> int:
    import getpass

    if _request("ping", {}) is None:  # before asking for a secret
        print("glanced is not running", file=sys.stderr)
        return EXIT_NOT_RUNNING
    key = getpass.getpass("API key (input hidden): ").strip()
    if not key:
        print("glance: no key entered", file=sys.stderr)
        return 2
    payload: dict[str, Any] = {"key": key}
    if provider:
        payload["provider"] = provider
    reply, code = _ask("backend_key", payload, SLOW_REQUEST_S)
    if reply is None:
        return code
    if code:
        return _refused(reply)
    print("key stored in the system keyring; run: glance backend use api")
    return 0


def _rewrite(tone: str) -> int:
    text = sys.stdin.read()
    if not text.strip():
        print("glance: no text on stdin", file=sys.stderr)
        return 2
    reply, code = _ask("rewrite", {"tone": tone, "text": text}, SLOW_REQUEST_S)
    if reply is None:
        return code
    if code:
        if reply.get("error") == "none":
            print(f"glance: {_NO_AI}", file=sys.stderr)
            return EXIT_NO_AI
        return _refused(reply)
    print(reply["text"])
    return 0


def _usage_offline(days: int | None) -> dict[str, Any]:
    """The report straight from the ledger file, for when glanced is not running."""
    from glance.backends.usage import UsageLedger
    from glance.config import ConfigError, load_config
    from glance.paths import config_path, usage_ledger_path, usage_path

    try:
        cfg = load_config(config_path())
    except ConfigError:
        from glance.config import Config

        cfg = Config()
    ledger = UsageLedger(usage_ledger_path())
    used = 0
    try:
        data = json.loads(usage_path().read_text(encoding="utf-8"))
        if data.get("date") == date.today().isoformat() and type(data.get("count")) is int:
            used = data["count"]
    except (OSError, ValueError, AttributeError):
        pass
    limit = cfg.ai.monthly_cost_limit_usd
    month = ledger.month_cost()
    return {"ok": True, "periods": ledger.periods(days),
            "budget": {"used_today": used, "per_day": cfg.backend.per_day,
                       "per_minute": cfg.backend.per_minute},
            "monthly_cost_limit_usd": limit, "month_cost_usd": round(month, 6),
            "limit_reason": "month" if limit is not None and month >= limit else None}


def _usage(days: int | None, as_json: bool) -> int:
    from glance.backends.usage import render_report

    reply = _request("usage", {"days": days} if days is not None else {})
    if reply is not None and not reply.get("ok"):
        return _refused(reply)
    report = reply if reply is not None else _usage_offline(days)
    report = {k: v for k, v in report.items() if k not in ("id", "ok")}
    print(json.dumps(report) if as_json else render_report(report))
    return 0


def _profile(action: str, name: str | None) -> int:
    msg = {"show": "profile_show", "set": "profile_set", "clear": "profile_clear"}[action]
    reply, code = _ask(msg, {"name": name} if action == "set" else {})
    if reply is None:
        return code
    if code:
        return _refused(reply)
    if action == "show":
        print(f"{reply['profile']} ({reply.get('source', '')}) for {reply.get('key', '')}")
    else:
        print("cleared" if action == "clear" else f"profile: {reply.get('profile', name)}")
    return 0


def _zshrc() -> Path:
    return Path(os.environ.get("ZDOTDIR") or os.path.expanduser("~")) / ".zshrc"


def _wrapper(action: str) -> int:
    z = _zshrc()
    if action == "status":
        print(claude_wrapper.status(z))
        return 0
    if action == "uninstall":
        print("removed" if claude_wrapper.uninstall(z) else "not installed")
        return 0
    if claude_wrapper.is_corrupt(z):
        raise ValueError(
            f"{z}: found a glance wrapper start marker without its end; fix it by hand"
        )
    if claude_wrapper.install(z) == "conflict":
        found = claude_wrapper.conflict_line(z)
        line = f"{z}:{found[0]}: {found[1]}" if found else str(z)
        print(f"conflict: claude is already defined at {line}", file=sys.stderr)
        print("Add this to your shell config by hand:", file=sys.stderr)
        print(
            f"{claude_wrapper.BEGIN}\n{claude_wrapper.SNIPPET}\n{claude_wrapper.END}",
            file=sys.stderr,
        )
        return 2
    print(f"installed in {z}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    from glance.doctor.checks import APP_ID, CHECKS
    from glance.doctor.runner import (
        apply_fixes,
        default_env,
        exit_code,
        render,
        run_checks,
        to_json,
        undo_all,
    )

    bad = [a for a in args.app or [] if not APP_ID.fullmatch(a)]
    if bad:
        print(f"glance: not an app id: {bad[0]!r}", file=sys.stderr)
        return 2
    env = default_env()
    # >>> macOS: its own checks (Accessibility, launchd, PyObjC); src/glance/platform/macos
    if sys.platform == "darwin":
        from glance.platform.macos.doctor import CHECKS, mac_env

        env = mac_env()
    # <<< macOS
    if args.undo:
        undo_all(env, CHECKS)
    if not args.json:
        from glance.version import version

        print(f"Glance {version()}", flush=True)
    # Each line as soon as its check is done, and a note before the slow ones: the AI CLI
    # checks can take seconds on a cold start, and a silent terminal looks like a hang.
    show = None if args.json else (lambda r: print(render([r]), flush=True))
    note = None if args.json else _progress
    if args.fix:
        if note is not None:
            note("Repairing what Glance can fix...")
        results = apply_fixes(env, CHECKS, args.app or [], args.skip or [], show, note)
    else:
        results = run_checks(env, CHECKS, show, note)
    if args.json:
        print(json.dumps(to_json(results)))
    return exit_code(results)


def _update(check_only: bool, main: bool) -> int:
    from glance import update

    if check_only:
        _progress("Checking for a newer Glance...")
        line, code = update.report(update.check())
        print(line)
        return code
    packaged = update.packaged_by()
    if packaged:
        manager, _, command = packaged.partition("|")
        print(f"Glance was installed with {manager}; update it there"
              + (f": {command}" if command else "."), file=sys.stderr)
        return update.EXIT_PACKAGED
    return update.run_installer(ref="main" if main else "latest")


def _features(name: str | None, state: str | None) -> int:
    """List the switches, show one, or turn one on/off. The config file is written directly
    (keeping its layout); a running glanced picks it up within a second."""
    from glance import feature_switches as fs
    from glance.config import Config, ConfigError, load_config, update_config
    from glance.paths import config_path

    path = config_path()
    try:
        cfg = load_config(path)
    except ConfigError as e:
        if state is not None:
            print(f"glance: {e}", file=sys.stderr)
            return 1
        cfg = Config()
    if name is None:
        print("\n".join(fs.listing(cfg)))
        return 0
    f = fs.feature(name)
    if state is None:
        print(f"{f.name}: {'on' if f.is_on(cfg) else 'off'}")
        return 0
    if not fs.set_feature(update_config, path, name, state == "on"):
        print(f"glance: could not write {path}", file=sys.stderr)
        return 1
    print(f"{f.name}: {state}")
    return 0


def _settings(section: str | None) -> int:
    """Open the Settings window: in the running glanced (one window, its live config), else
    in this process on its own."""
    reply = _request("settings_open", {"section": section} if section else {})
    if reply is not None and reply.get("ok"):
        return 0
    if reply is not None and reply.get("error") != "unknown request":
        return _refused(reply)
    # >>> macOS: the AppKit window; src/glance/platform/macos
    if sys.platform == "darwin":
        from glance.platform.macos.settings import main as mac_settings

        return mac_settings(section)
    # <<< macOS
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("glance: no display to open the Settings window on", file=sys.stderr)
        return 1
    from glance.ui.settings_gtk import main as gtk_settings

    return gtk_settings(section)


def main(argv: list[str] | None = None) -> int:
    from glance.log import quiet

    with quiet():  # log lines go to glanced's log, never to the user's terminal
        return _main(argv)


def _main(argv: list[str] | None) -> int:
    from glance.version import version

    ap = argparse.ArgumentParser(prog="glance")
    ap.add_argument("--version", action="version", version=f"Glance {version()}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wrapper", help="claude zsh wrapper for Ctrl+G")
    w.add_argument("action", choices=["install", "uninstall", "status"])
    sub.add_parser("start", help="start Glance again (after Quit in the tray)")
    st = sub.add_parser("status", help="what glanced is doing")
    st.add_argument("--json", action="store_true", help="print one JSON object")
    pa = sub.add_parser("pause", help="stop checking until resumed or for DURATION")
    pa.add_argument("duration", nargs="?", metavar="DURATION",
                    help="30m, 2h, 45s; a bare number is minutes")
    sub.add_parser("resume", help="start checking again")
    sub.add_parser("shot", help="take a Glance Shot: pick a screen region and act on it")
    d = sub.add_parser("doctor", help="check the setup; --fix repairs what Glance owns")
    mode = d.add_mutually_exclusive_group()
    mode.add_argument("--fix", action="store_true", help="repair fixable problems")
    mode.add_argument("--undo", action="store_true", help="remove what --fix installed")
    d.add_argument("--app", action="append", metavar="ID",
                   help="with --fix: also opt this Chromium/Electron app in (repeatable)")
    d.add_argument("--skip", action="append", metavar="ID",
                   help="with --fix: leave this check alone (repeatable)")
    d.add_argument("--json", action="store_true", help="print the results as JSON")
    b = sub.add_parser("backend", help="choose and test the AI Glance uses")
    bsub = b.add_subparsers(dest="backend_cmd", required=True)
    bsub.add_parser("list", help="which AIs are connected, and how to connect the rest")
    bu = bsub.add_parser("use", help="use this AI")
    bu.add_argument("name", choices=BACKENDS)
    bt = bsub.add_parser("test", help="one real call, to see that it works")
    bt.add_argument("name", nargs="?", choices=BACKENDS)
    bk = bsub.add_parser("set-key", aliases=["key"], help="store an API key in the keyring")
    bk.add_argument("--provider", choices=("anthropic", "openai"))
    bm = bsub.add_parser("model", help="the model a backend uses (default: its fastest, cheapest)")
    bm.add_argument("name", choices=BACKENDS)
    bm.add_argument("model", nargs="?", help="model name, as the tool spells it")
    bm.add_argument("--default", action="store_true", help="go back to Glance's default model")
    us = sub.add_parser("usage", help="how much AI Glance used (calls, tokens, cost)")
    us.add_argument("--days", type=int, metavar="N", help="just the last N days (1-90)")
    us.add_argument("--json", action="store_true", help="print the numbers as JSON")
    rw = sub.add_parser("rewrite", help="rewrite stdin in a tone and print it")
    rw.add_argument("--tone", required=True, choices=_TONE_CHOICES)
    pr = sub.add_parser("profile", help="the focused field's audience profile")
    pr.add_argument("action", nargs="?", default="show", choices=["show", "set", "clear"])
    pr.add_argument("name", nargs="?")
    up = sub.add_parser("update", help="install the latest release (re-runs the installer)")
    up.add_argument("--check", action="store_true",
                    help="only say whether an update exists (exit 10 if so)")
    up.add_argument("--main", action="store_true",
                    help="install the main branch (the bleeding edge) instead of a release")
    se = sub.add_parser("setup", help="per-user setup after a package install")
    se.add_argument("--undo", action="store_true", help="remove what setup did for this user")
    se.add_argument("--no-start", action="store_true", help="enable at login, do not start now")
    rp = sub.add_parser("report", help="write a diagnostics file for a bug report")
    rp.add_argument("--stdout", action="store_true", help="print it instead of writing a file")
    rp.add_argument("--output", "-o", metavar="FILE", help="where to write it")
    from glance.feature_switches import NAMES as FEATURE_NAMES

    fe = sub.add_parser("features", help="list the features, or turn one on or off")
    fe.add_argument("name", nargs="?", choices=FEATURE_NAMES)
    fe.add_argument("state", nargs="?", choices=("on", "off"))
    se = sub.add_parser("settings", help="open the Settings window")
    se.add_argument("section", nargs="?", choices=SETTINGS_SECTIONS)
    args = ap.parse_args(argv)
    if args.cmd == "settings":
        return _settings(args.section)
    if args.cmd == "start":
        from glance import service

        return service.main(running=lambda: _request("status", {}) is not None)
    if args.cmd == "features":
        return _features(args.name, args.state)
    if args.cmd in ("status", "pause", "resume"):
        try:
            return _control(args)
        except ValueError as e:
            print(f"glance: {e}", file=sys.stderr)
            return 2
    if args.cmd == "backend":
        if args.backend_cmd == "list":
            return _backend_list()
        if args.backend_cmd == "use":
            return _backend_use(args.name)
        if args.backend_cmd == "test":
            return _backend_test(args.name)
        if args.backend_cmd == "model":
            return _backend_model(args.name, args.model, args.default)
        return _backend_set_key(args.provider)
    if args.cmd == "usage":
        if args.days is not None and not 1 <= args.days <= 90:
            ap.error("--days must be between 1 and 90")
        return _usage(args.days, args.json)
    if args.cmd == "rewrite":
        return _rewrite(args.tone)
    if args.cmd == "profile":
        if (args.action == "set") != (args.name is not None):
            ap.error("usage: glance profile [show | set NAME | clear]")
        return _profile(args.action, args.name)
    if args.cmd == "update":
        return _update(args.check, args.main)
    if args.cmd == "setup":
        from glance import setup

        return setup.main(undo=args.undo, no_start=args.no_start)
    if args.cmd == "report":
        from glance import report

        return report.main(to_stdout=args.stdout, output=args.output)
    if args.cmd == "shot":
        return _shot()
    if args.cmd == "doctor":
        return _doctor(args)
    if args.cmd == "wrapper":
        try:
            return _wrapper(args.action)
        except (OSError, ValueError) as e:
            print(f"glance: {e}", file=sys.stderr)
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
