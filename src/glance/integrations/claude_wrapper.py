"""Install/remove the zsh `claude` wrapper that points Ctrl+G at glance-edit.

The wrapper scopes EDITOR/VISUAL to the one `claude` process only; the shell's own
EDITOR and VISUAL never change, so git and everything else keep the real editor.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

BEGIN = "# >>> glance claude wrapper >>>"
END = "# <<< glance claude wrapper <<<"
SNIPPET = (
    "claude() {\n"
    '  GLANCE_REAL_EDITOR="${VISUAL:-${EDITOR:-}}" VISUAL=glance-edit EDITOR=glance-edit'
    ' command claude "$@"\n'
    "}"
)
BLOCK = f"{BEGIN}\n{SNIPPET}\n{END}\n"

_CONFLICT = re.compile(r"^\s*(?:function\s+claude\b|claude\s*\(\s*\)|alias\s+claude=)")


def _read(zshrc: Path) -> str:
    try:
        with open(zshrc, encoding="utf-8", errors="surrogateescape", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _split(text: str) -> tuple[str, str, str] | None:
    """(before, block, after) for the marked block, or None."""
    b = text.find(BEGIN)
    if b < 0:
        return None
    e = text.find(END, b)
    if e < 0:
        return None
    e += len(END)
    if text[e : e + 1] == "\n":
        e += 1
    return text[:b], text[b:e], text[e:]


def is_corrupt(zshrc: Path) -> bool:
    """A BEGIN marker without a matching END after it."""
    text = _read(zshrc)
    b = text.find(BEGIN)
    return b >= 0 and text.find(END, b) < 0


def conflict_line(zshrc: Path) -> tuple[int, str] | None:
    """First (1-based line, text) of a claude definition outside the marked block."""
    text = _read(zshrc)
    parts = _split(text)
    if parts:
        before, block, _after = parts
        skip_from, skip_to = len(before), len(before) + len(block)
    else:
        skip_from = skip_to = -1
    pos = 0
    for n, line in enumerate(text.splitlines(keepends=True), 1):
        inside = skip_from <= pos < skip_to
        pos += len(line)
        if not inside and _CONFLICT.match(line):
            return n, line.rstrip("\n")
    return None


def status(zshrc: Path) -> str:
    if is_corrupt(zshrc) or conflict_line(zshrc):
        return "conflict"
    return "installed" if _split(_read(zshrc)) else "absent"


def _write_atomic(zshrc: Path, text: str) -> None:
    target = Path(os.path.realpath(zshrc))
    try:
        mode = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o644 & ~umask
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".zshrc.glance-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def install(zshrc: Path) -> str:
    if is_corrupt(zshrc) or conflict_line(zshrc):
        return "conflict"
    text = _read(zshrc)
    if _split(text):
        return "installed"
    # One blank separator line (or just a line terminator if the file lacked one);
    # uninstall removes exactly that.
    prefix = "\n" if text else ""
    _write_atomic(zshrc, text + prefix + BLOCK)
    return "installed"


def uninstall(zshrc: Path) -> bool:
    if is_corrupt(zshrc):
        raise ValueError(f"{zshrc}: found '{BEGIN}' without '{END}'; fix it by hand")
    text = _read(zshrc)
    parts = _split(text)
    if not parts:
        return False
    before, _block, after = parts
    if before.endswith("\n\n"):
        before = before[:-1]  # the blank separator the installer added
    elif before.endswith("\n"):
        before = before[:-1]  # the line terminator added to a file lacking one
    _write_atomic(zshrc, before + after)
    return True
