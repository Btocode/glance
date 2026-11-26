"""Run the checks, apply opt-in fixes, render the report."""

from __future__ import annotations

import dataclasses
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from glance.doctor.checks import Check, CheckResult, DoctorEnv
from glance.log import log_event

MARK = {"ok": "✓", "warn": "!", "fail": "✗"}


def _run_one(check: Check, env: DoctorEnv) -> CheckResult:
    try:
        return check.run(env)
    except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
        return CheckResult(check.id, check.id, "fail", f"check crashed: {e.__class__.__name__}")


def run_checks(env: DoctorEnv, checks: Sequence[Check],
               on_result: Callable[[CheckResult], None] | None = None,
               on_start: Callable[[str], None] | None = None) -> list[CheckResult]:
    """Every check in order. `on_result` hears each result as soon as it is known (the CLI
    prints it then, not the whole report at the end); `on_start` hears a check's `progress`
    line before one that can take seconds starts."""
    out = []
    for check in checks:
        progress = getattr(check, "progress", None)
        if progress and on_start is not None:
            on_start(progress)
        result = _run_one(check, env)
        out.append(result)
        if on_result is not None:
            on_result(result)
    return out


def apply_fixes(env: DoctorEnv, checks: Sequence[Check], apps: Sequence[str],
                skip: Sequence[str] = (),
                on_result: Callable[[CheckResult], None] | None = None,
                on_start: Callable[[str], None] | None = None) -> list[CheckResult]:
    """Run `fix` for every non-ok check not in `skip`, then re-run everything (the re-run
    is what `on_result` / `on_start` hear)."""
    fix_env = dataclasses.replace(env, apps=tuple(apps))
    for check, result in zip(checks, run_checks(fix_env, checks), strict=True):
        wanted = result.status != "ok" or (apps and getattr(check, "opt_in", False))
        if wanted and check.id not in skip:
            try:
                check.fix(fix_env)
            except Exception as e:  # noqa: BLE001 - reported by the re-run below
                log_event("doctor_fix_error", check=check.id, error=e.__class__.__name__)
    if on_start is not None:
        on_start("Checking again...")
    return run_checks(env, checks, on_result, on_start)


def undo_all(env: DoctorEnv, checks: Sequence[Check]) -> None:
    for check in checks:
        try:
            check.undo(env)
        except Exception:  # noqa: BLE001, S110 - keep undoing the rest
            pass


def render(results: Sequence[CheckResult]) -> str:
    lines: list[str] = []
    for r in results:
        lines.append(f"{MARK[r.status]} {r.title} — {r.detail}")
        if r.hint:
            lines += ["    " + h for h in r.hint.splitlines()]
    return "\n".join(lines)


def exit_code(results: Sequence[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0


def to_json(results: Sequence[CheckResult]) -> list[dict]:
    return [dataclasses.asdict(r) for r in results]


def default_env() -> DoctorEnv:
    """The real thing: the user's home, environment, processes and glanced."""
    from glance.cli import _request
    from glance.update import check as update_status

    def run(argv: list[str], timeout: float | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout or 15,
                               check=False)
        except (OSError, subprocess.TimeoutExpired):
            return 127, ""
        return p.returncode, p.stdout + (p.stderr if p.returncode else "")

    def procs() -> list[tuple[int, list[str]]]:
        out = []
        for d in Path("/proc").iterdir():
            if d.name.isdigit():
                try:
                    argv = (d / "cmdline").read_bytes().decode(errors="replace").split("\0")
                except OSError:
                    continue
                if argv and argv[0]:
                    out.append((int(d.name), [a for a in argv if a]))
        return out

    def which(name: str) -> str | None:
        import shutil
        return shutil.which(name)

    environ = dict(os.environ)
    dirs = environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    environ["XDG_DATA_DIRS"] = dirs + ":/var/lib/snapd/desktop"  # snap launchers live there
    return DoctorEnv(home=Path.home(), environ=environ, run=run, procs=procs,
                     ctl=_request, which=which, update_status=update_status)
