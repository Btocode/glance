"""glance-edit: `$EDITOR` for Claude Code's Ctrl+G, and nobody else's.

Claude Code 2.1.x runs `$EDITOR /tmp/claude-<uid>/claude-prompt-<uuid>.md` and then looks
only at the exit code and the file (findings S7.1): exit 0 with a changed file applies it,
exit 0 with the file untouched cancels, any other exit keeps the prompt as typed. So:

* not called by Claude Code, or anything unexpected before the window is up (no daemon, a
  daemon that does not answer, an unreadable file, a strange reply, the daemon dying while
  the window is open): become the user's real editor, with the same argv;
* an unexpected error of our own: exit 1 with the file untouched (the prompt is kept).

The prompt text goes to `glanced` over its private socket and nowhere else. The shim
writes nothing but the file it was given (through a temp file beside it, for atomicity)
and logs nothing.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

PROMPT_NAME = re.compile(r"^claude-prompt-[0-9a-f-]+\.md$")
# Set by Claude Code itself for the editor (S7.1). CLAUDECODE is inherited by nested shells
# and so says nothing (S7.2).
CLAUDE_ENV = "CLAUDE_CODE_ENTRYPOINT"
SELF = "glance-edit"
FALLBACK_EDITORS = ("sensible-editor", "nano", "vi")
MAX_ANCESTORS = 8
CONNECT_TIMEOUT_S = 0.5
PING_TIMEOUT_S = 1.0  # the daemon's main loop answers pings: a stuck one is caught here
ACTIONS = ("apply", "editor", "cancel")
SESSION_TIMEOUT_S = 30 * 60  # nobody edits one prompt for longer: then the real editor opens
# Set in the real editor's environment. A glance-edit that starts with it set was started,
# one way or another, by its own fallback: it must not try again, or it would exec forever.
GUARD = "GLANCE_EDIT_DEPTH"
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_execvpe = os.execvpe  # replaced by the tests: a real exec never returns


def is_claude_code(env: Mapping[str, str], ancestors: Sequence[str], path: str) -> bool:
    """Called by Claude Code for its prompt file (S7.2): the file name is Claude Code's, and
    Claude Code set its entrypoint variable or is one of our ancestors. The name alone
    separates a `git commit` run by Claude's own shell tool, which inherits both."""
    if not PROMPT_NAME.match(os.path.basename(path)):
        return False
    return CLAUDE_ENV in env or "claude" in ancestors


def ancestors() -> list[str]:
    """`comm` of the parent, grandparent, ... (at most 8), from /proc."""
    out: list[str] = []
    pid = os.getppid()
    while pid > 0 and len(out) < MAX_ANCESTORS:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            stat = Path(f"/proc/{pid}/stat").read_text()
            # "pid (comm) state ppid ...": comm may hold spaces and parentheses.
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            break
        out.append(comm)
        pid = ppid
    return out


def _is_self(value: str) -> bool:
    """Does this editor command run glance-edit? `env [-opts] [VAR=x] cmd` counts as cmd."""
    try:
        words = shlex.split(value)
    except ValueError:
        return False
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-")]
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1:]
    return bool(words) and os.path.basename(words[0]) == SELF


def _fallback(env: Mapping[str, str], editors: Sequence[str]) -> list[str]:
    for name in editors:
        if shutil.which(name, path=env.get("PATH")):
            return [name]
    return ["vi"]


def real_editor(env: Mapping[str, str]) -> list[str]:
    """The editor the user would have had without Glance."""
    for name in ("GLANCE_REAL_EDITOR", "VISUAL", "EDITOR"):
        value = env.get(name, "").strip()
        if not value:
            continue
        try:
            words = shlex.split(value)
        except ValueError:
            continue
        if words and not _is_self(value):
            return words
    return _fallback(env, FALLBACK_EDITORS)


def _forced(env: Mapping[str, str]) -> bool:
    """Tests may skip the "is it Claude Code?" check, and only tests."""
    return env.get("GLANCE_TEST") == "1" and env.get("GLANCE_TEST_FORCE_CLAUDE") == "1"


def _ctl_sock(env: Mapping[str, str]) -> Path:
    # As glance.paths.ctl_sock(), without creating the directory: the shim writes nothing.
    base = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "glance" / "ctl.sock"


def _window(env: Mapping[str, str]) -> int:
    value = env.get("WINDOWID", "")
    return int(value) if value.isdigit() else 0


class _FallThrough(Exception):
    """Something is not right: the user gets their real editor."""


def _ask_daemon(env: Mapping[str, str], text: str) -> tuple[str, str]:
    """(action, text) from glanced, or _FallThrough. Blocks while the window is open."""
    from glance.ipc import JsonLineClient

    client = JsonLineClient(_ctl_sock(env))
    try:
        try:
            client.connect(timeout_s=CONNECT_TIMEOUT_S)
            client.request("ping", {}, timeout_s=PING_TIMEOUT_S)
            reply = client.request("edit_session", {"text": text, "window": _window(env)},
                                   timeout_s=SESSION_TIMEOUT_S)
        except (ConnectionError, TimeoutError, OSError) as e:
            raise _FallThrough from e
    finally:
        client.close()
    action, new = reply.get("action"), reply.get("text")
    if action not in ACTIONS or (action != "cancel" and not isinstance(new, str)):
        raise _FallThrough
    return action, new if isinstance(new, str) else ""


def _write(path: Path, original: str, text: str) -> None:
    """Replace the file's content with `text`, keeping the original's trailing-newline state
    (S7.1: never add a newline it lacked). Atomic: the file is either old or new."""
    body = text.rstrip("\n") + ("\n" if original.endswith("\n") else "")
    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".glance", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The real editor's environment: no VISUAL/EDITOR that would run us again (a
    sensible-editor reads them), and the guard."""
    out = {k: v for k, v in env.items()
           if not (k in ("VISUAL", "EDITOR", "GLANCE_REAL_EDITOR") and _is_self(v))}
    out[GUARD] = "1"
    return out


def _exec_editor(env: Mapping[str, str], args: list[str]) -> int:
    if env.get(GUARD):
        # Our own fallback brought us back: a plain editor, never one that picks for itself.
        argv = _fallback(env, [e for e in FALLBACK_EDITORS if e != "sensible-editor"]) + args
    else:
        argv = real_editor(env) + args
    try:
        _execvpe(argv[0], argv, _child_env(env))
    except OSError:
        return 127
    return 127  # pragma: no cover - only reached with a spy that returns


def _run(args: list[str]) -> int:
    env = dict(os.environ)
    if env.get(GUARD):
        return _exec_editor(env, args)
    files = [a for a in args if not a.startswith(("-", "+"))]
    if len(files) != 1:
        return _exec_editor(env, args)
    path = Path(files[0])
    if not _forced(env) and not is_claude_code(env, ancestors(), str(path)):
        return _exec_editor(env, args)
    try:
        original = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return _exec_editor(env, args)
    shown = original.removesuffix("\n")
    try:
        action, text = _ask_daemon(env, shown)
    except _FallThrough:
        return _exec_editor(env, args)
    if action == "cancel":
        return 0
    if text != shown:
        _write(path, original, text)
    if action == "editor":
        return _exec_editor(env, args)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001 - any failure of ours: keep the prompt, say nothing
        return 1


if __name__ == "__main__":
    sys.exit(main())
