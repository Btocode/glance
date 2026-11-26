"""`glance report`: a diagnostics file to attach to a bug report.

What goes in: versions, OS/desktop/session, doctor results, which AIs are installed and
signed in, feature switches, the config with every value that could be personal replaced, the
last few hundred glanced log lines reduced to event names and numeric or enumerated fields,
crash and forced-exit counts, and timing percentiles.

What never goes in: typed or selected text, the clipboard, window titles, URLs, API keys,
the user name, the host name and home paths (written as ~). Every line passes `scrub` last,
so a value that slips through a section is still caught."""
from __future__ import annotations

import getpass
import json
import os
import platform
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

ISSUES_URL = "https://github.com/Btocode/glance/issues/new/choose"
LOG_LINES = 300
_EVENT = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_ENUM = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_ERROR_KEYS = {"kind", "error", "exc", "reason"}
_CLASS_NAME = re.compile(r"^[A-Z][A-Za-z0-9_]{0,40}$")  # an exception type: TimeoutError
_CONFIG_TOKEN = re.compile(r"^[A-Za-z0-9_.+-]{1,40}$")
# Fields that name a person's apps, files or windows: dropped even when they look like a token.
_IDENTITY_KEYS = {"wm_class", "app", "app_id", "window", "title", "url", "uri", "path", "file",
                  "dir", "user", "home", "host", "cmd", "argv", "name", "text", "page", "key",
                  "value", "word", "lang_text", "role_name", "toolkit", "desktop_file", "model"}
_SECRET_KEY = re.compile(r"(key|token|secret|password|passwd|credential|auth)", re.IGNORECASE)
_CRASH_MARKERS = (
    ("python_traceback", "Traceback (most recent call last)"),
    ("forced_exit", "glanced_forced_exit"),
    ("unit_failed", "Failed with result"),
    ("killed_by_signal", "code=killed"),
    ("core_dumped", "code=dumped"),
    ("child_died", "child_died"),
)


# ------------------------------------------------------------------ scrubbing

def scrub(text: str, home: str, user: str, host: str) -> str:
    """Home paths to ~, the user and host names to <user>/<host>, any secret-shaped token to
    <secret>."""
    if home and home not in ("/", ""):
        text = text.replace(home.rstrip("/"), "~")
    for name, mark in ((user, "<user>"), (host, "<host>")):
        if name and len(name) >= 2:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", mark, text)
    text = re.sub(r"/(home|Users)/[^/\s]+", "~", text)
    try:
        from glance.privacy.redact import find_secrets

        for span in sorted(find_secrets(text), key=lambda s: s.start, reverse=True):
            text = text[:span.start] + "<secret>" + text[span.end:]
    except Exception:  # noqa: BLE001, S110 - the regexes above still ran
        pass
    return text


def filter_log_line(line: str) -> str | None:
    """`event k=v ...` reduced to the event and its numeric or enumerated fields; None for
    anything that is not one of glanced's event lines (tracebacks, free text)."""
    if not line or line[0].isspace():
        return None
    words = line.rstrip().split()
    # glanced's lines are exactly `event k=v k=v` (log.py never writes a space in a value):
    # a line with any bare word after the first is free text, and so is a lone word without _
    if not words or not _EVENT.match(words[0]) or any("=" not in w for w in words[1:]) or (
            len(words) == 1 and "_" not in words[0]):
        return None
    kept = [words[0]]
    for w in words[1:]:
        k, sep, v = w.partition("=")
        if not sep or not _KEY.match(k) or k in _IDENTITY_KEYS:
            continue
        if _NUMBER.match(v) or v in ("True", "False", "None") or (
                _ENUM.match(v) and not _SECRET_KEY.search(k)) or (
                k in _ERROR_KEYS and _CLASS_NAME.match(v)):
            kept.append(f"{k}={v}")
    return " ".join(kept)


def crash_markers(lines: Iterable[str]) -> dict[str, int]:
    counts = {name: 0 for name, _ in _CRASH_MARKERS}
    for line in lines:
        for name, needle in _CRASH_MARKERS:
            if needle in line:
                counts[name] += 1
    return counts


def _percentile(values: Sequence[float], pct: float) -> float:
    s = sorted(values)
    i = min(len(s) - 1, max(0, round(pct / 100 * (len(s) - 1))))
    return s[i]


def timings(lines: Iterable[str]) -> dict[str, dict[str, float]]:
    """p50/p95/max per `event.field` for millisecond fields (`ms`, `*_ms`) in filtered lines."""
    seen: dict[str, list[float]] = {}
    for line in lines:
        words = line.split()
        for w in words[1:]:
            k, _, v = w.partition("=")
            if (k == "ms" or k.endswith("_ms")) and _NUMBER.match(v):
                seen.setdefault(f"{words[0]}.{k}", []).append(float(v))
    return {k: {"n": len(v), "p50": _percentile(v, 50), "p95": _percentile(v, 95),
                "max": max(v)} for k, v in sorted(seen.items())}


def redact_config(value: Any, key: str = "") -> Any:
    """Booleans and numbers kept; secrets, paths and free text replaced."""
    if isinstance(value, Mapping):
        return {str(k): redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_config(v, key) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if _SECRET_KEY.search(key):
        return "<redacted>"
    s = str(value)
    if "/" in s or "\\" in s or s.startswith("~"):
        return "<path>"
    if not _CONFIG_TOKEN.match(s):
        return "<text>"
    return s


# ------------------------------------------------------------------ sources

@dataclass
class Sources:
    versions: dict[str, str]
    system: dict[str, str]
    doctor: list[tuple[str, str, str]]           # (id, status, detail)
    backends: dict[str, dict[str, Any]] | None   # name -> {installed, signed_in}; None: down
    choice: str | None
    features: list[str]
    config: dict[str, Any] | None
    log: list[str]                               # raw glanced lines, oldest first
    home: str = ""
    user: str = ""
    host: str = ""
    notes: list[str] = field(default_factory=list)


def build(src: Sources) -> str:
    out: list[str] = [f"Glance diagnostics report ({date.today().isoformat()})",
                      ("No typed text, clipboard, window titles, URLs, keys or user names "
                       "are included."), ""]

    def section(title: str, lines: Iterable[str]) -> None:
        out.append(f"== {title}")
        out.extend(lines)
        out.append("")

    section("Versions", (f"{k}: {v}" for k, v in src.versions.items()))
    section("System", (f"{k}: {v}" for k, v in src.system.items()))
    section("Doctor", (f"[{st}] {cid}: {detail}" for cid, st, detail in src.doctor))
    if src.backends is None:
        section("AI backends", ["glanced is not running; not asked"])
    else:
        section("AI backends", [f"in use: {src.choice or 'none'}"] + [
            f"{n}: installed={'yes' if b.get('installed') else 'no'} "
            f"signed_in={ {True: 'yes', False: 'no'}.get(b.get('signed_in'), 'unknown') }"
            for n, b in sorted(src.backends.items())])
    section("Features", src.features)
    section("Config (values that could be personal are replaced)",
            ["defaults (no config file)"] if src.config is None
            else json.dumps(redact_config(src.config), indent=2, sort_keys=True).splitlines())
    filtered = [f for f in (filter_log_line(x) for x in src.log) if f]
    section("Crash and forced-exit markers (whole log tail)",
            (f"{k}: {v}" for k, v in crash_markers(src.log).items()))
    stats = timings(filtered)
    section("Timings (ms, from log events)",
            [f"{k}: n={v['n']} p50={v['p50']:g} p95={v['p95']:g} max={v['max']:g}"
             for k, v in stats.items()] or ["no timing events in the log"])
    section(f"glanced log (last {LOG_LINES} event lines; event names and numeric or "
            "enumerated fields only)", filtered[-LOG_LINES:] or ["(empty)"])
    if src.notes:
        section("Notes", src.notes)
    return "\n".join(scrub(line, src.home, src.user, src.host) for line in out) + "\n"


# ------------------------------------------------------------------ collecting

def _run(argv: list[str], timeout: float = 10) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout


def _versions() -> dict[str, str]:
    from glance.paths import harper_bin
    from glance.version import version

    out = {"glance": version()}
    try:
        from glance.grammar import harper_native as hn

        out["glance_core"] = hn.core_version() or "not installed"
        out["harper (in glance_core)"] = (hn.harper_version() or "n/a") if hn.available() \
            else "n/a"
    except Exception:  # noqa: BLE001
        out["glance_core"] = "unknown"
    hb = harper_bin()
    out["harper-ls"] = (_run([str(hb), "--version"], 5).strip() or "present") if hb.exists() \
        else "not installed"
    from glance.update import packaged_by

    pkg = packaged_by()
    out["installed by"] = pkg.partition("|")[0] if pkg else "installer (curl | bash) or checkout"
    return out


def _system() -> dict[str, str]:
    env = os.environ
    osname = platform.system()
    pretty = ""
    if osname == "Linux":
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip('"')
        except OSError:
            pass
    elif osname == "Darwin":
        pretty = "macOS " + platform.mac_ver()[0]
    out = {"os": pretty or osname, "kernel": platform.release(), "arch": platform.machine(),
           "python": platform.python_version(),
           "session": env.get("XDG_SESSION_TYPE", "") or ("aqua" if osname == "Darwin" else "?"),
           "desktop": env.get("XDG_CURRENT_DESKTOP", "") or "?"}
    if "GNOME" in out["desktop"].upper():
        out["gnome-shell"] = _run(["gnome-shell", "--version"], 5).strip() or "?"
    return out


def _doctor() -> list[tuple[str, str, str]]:
    try:
        if sys.platform == "darwin":
            from glance.platform.macos.doctor import CHECKS, mac_env

            env = mac_env()
        else:
            from glance.doctor.checks import CHECKS
            from glance.doctor.runner import default_env

            env = default_env()
        from glance.doctor.runner import run_checks

        return [(r.id, r.status, r.detail) for r in run_checks(env, CHECKS)]
    except Exception as e:  # noqa: BLE001 - a report is still useful without doctor
        return [("doctor", "fail", f"could not run: {type(e).__name__}")]


def _backends() -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    from glance.cli import _request

    reply = _request("backend_list", {"refresh": False}, 20.0)
    if not reply or not reply.get("ok"):
        return None, None
    names = {n: {"installed": bool(b.get("installed")), "signed_in": b.get("signed_in")}
             for n, b in (reply.get("backends") or {}).items()}
    return names, reply.get("choice")


def _config() -> tuple[dict[str, Any] | None, list[str]]:
    from glance import feature_switches as fs
    from glance.config import Config, ConfigError, load_config
    from glance.paths import config_path

    path = config_path()
    raw = None
    if path.exists():
        try:
            import tomli

            raw = tomli.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            raw = {"(unreadable)": True}
    try:
        cfg = load_config(path)
    except ConfigError:
        cfg = Config()
    return raw, fs.listing(cfg)


def _log_lines(n: int = 2000) -> list[str]:
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Logs" / "Glance" / "glanced.log"
        try:
            return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []
    out = _run(["journalctl", "--user", "-u", "glance.service", "-n", str(n), "-o", "cat",
                "--no-pager"], 15)
    return out.splitlines()


def collect(progress: Callable[[str], None] = lambda s: None) -> Sources:
    progress("Collecting versions and system details...")
    versions, system = _versions(), _system()
    progress("Running glance doctor (a few seconds)...")
    doctor = _doctor()
    backends, choice = _backends()
    config, features = _config()
    progress("Reading glanced's log...")
    log = _log_lines()
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    return Sources(versions, system, doctor, backends, choice, features, config, log,
                   home=str(Path.home()), user=user, host=socket.gethostname())


def default_path(today: date | None = None) -> Path:
    return Path.home() / f"glance-report-{(today or date.today()).isoformat()}.txt"


def main(to_stdout: bool = False, output: str | None = None,
         collect_fn: Callable[..., Sources] | None = None) -> int:
    def progress(s: str) -> None:
        print(s, file=sys.stderr, flush=True)

    text = build((collect_fn or collect)(progress))
    if to_stdout:
        sys.stdout.write(text)
        return 0
    path = Path(output).expanduser() if output else default_path()
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"glance report: could not write {path}: {e}", file=sys.stderr)
        return 1
    shown = str(path).replace(str(Path.home()), "~", 1)
    print(f"Wrote {shown}")
    print("Read it, then attach it to an issue: " + ISSUES_URL)
    return 0
