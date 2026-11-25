"""Reversible `.desktop` overrides that add `--force-renderer-accessibility`.

System files are only ever read. Every file Glance writes in the user's applications dir carries
`X-Glance-Override=true`; any user file it replaces or edits is first copied byte-for-byte to
`<name>.glance-bak` and recorded in the manifest, so `undo_all` restores the exact previous bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from glance.log import log_event

FLAG = "--force-renderer-accessibility"
DEFAULT_IDS = ("google-chrome", "com.google.Chrome", "brave-browser", "brave-browser-nightly",
               "com.brave.Browser", "com.brave.Browser.nightly", "slack", "discord")
SYSTEM_DIRS = ("/usr/share/applications", "/var/lib/snapd/desktop/applications",
               "/usr/local/share/applications")
MARKER = "X-Glance-Override=true"
HASH_KEY = "X-Glance-Source-SHA256="
BACKUP_SUFFIX = ".glance-bak"
CHROMIUM_MARKERS = ("chrome-sandbox", "chrome_crashpad_handler", "resources/app.asar")
# Browser launchers whose user-owned copies (e.g. PWA shortcuts) get the flag in place.
BROWSER_BINARIES = frozenset({
    "google-chrome", "google-chrome-stable", "google-chrome-beta", "google-chrome-unstable",
    "brave", "brave-browser", "brave-browser-stable", "brave-browser-beta",
    "brave-browser-nightly"})

_EXEC = re.compile(r'''^(Exec=(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)?(?:"(?:[^"\\]|\\.)*"|\S+))''')


@dataclass(frozen=True)
class Target:
    id: str
    source: Path
    name: str
    binary: str
    default: bool


def rewrite_exec(line: str) -> str:
    """Insert FLAG right after the command word of an `Exec=` line; idempotent."""
    if not line.startswith("Exec=") or FLAG in line.split():
        return line
    m = _EXEC.match(line)
    if not m:
        return line
    return m.group(1) + " " + FLAG + line[m.end():]


def _read(path: Path, errors: str = "surrogateescape") -> str:
    """Text view that keeps every byte (non-UTF-8 survives, line endings untouched).

    Pass errors="replace" for text that is only displayed, never written back.
    """
    with open(path, encoding="utf-8", errors=errors, newline="") as f:
        return f.read()


def _enc(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _command_word(exec_value: str) -> str:
    m = _EXEC.match("Exec=" + exec_value)
    if not m:
        return ""
    rest = m.group(1)[len("Exec="):]
    words = re.findall(r'"(?:[^"\\]|\\.)*"|\S+', rest)
    if words and words[0] == "env":
        words = [w for w in words[1:] if not re.match(r"[A-Za-z_][A-Za-z0-9_]*=", w)]
    return words[-1].strip('"') if words else ""


def _entry(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    in_main = False
    for line in text.splitlines():
        if line.startswith("["):
            in_main = line.strip() == "[Desktop Entry]"
        elif in_main and "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out.setdefault(k, v)
    return out


def _is_chromium_app(binary: str) -> bool:
    if not binary:
        return False
    path = binary if "/" in binary else shutil.which(binary)
    if not path:
        return False
    d = Path(os.path.realpath(path)).parent
    return any((d / m).exists() for m in CHROMIUM_MARKERS)


def find_targets(system_dirs: Iterable[Path | str], user_dir: Path) -> list[Target]:
    """Default apps present on this machine plus detected Chromium/Electron apps."""
    seen: set[str] = set()
    out: list[Target] = []
    for d in map(Path, system_dirs):
        if not d.is_dir() or d == user_dir:
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.stem in seen:
                continue
            try:
                entry = _entry(_read(f, "replace"))
            except OSError:
                continue
            binary = _command_word(entry.get("Exec", ""))
            default = f.stem in DEFAULT_IDS
            if default or _is_chromium_app(binary):
                seen.add(f.stem)
                out.append(Target(f.stem, f, entry.get("Name", f.stem), binary, default))
    return out


def _atomic_write(path: Path, data: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".glance-tmp")
    tmp.write_bytes(data)
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def _load(manifest: Path) -> dict:
    try:
        data = json.loads(manifest.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("backups", [])
    return data


def _save(manifest: Path, data: dict) -> None:
    if not manifest.parent.exists():
        data["created_manifest_dir"] = True
    _atomic_write(manifest, json.dumps(data, indent=1).encode())


def _ensure_user_dir(user_dir: Path, manifest: Path) -> None:
    if not user_dir.exists():
        user_dir.mkdir(parents=True)
        data = _load(manifest)
        data["created_user_dir"] = True
        _save(manifest, data)


def _recorded_backup(path: Path, data: dict) -> Path | None:
    for b in data["backups"]:
        if b.get("path") == str(path):
            return Path(b["backup"])
    return None


def _backup(path: Path, manifest: Path) -> Path:
    """Copy a user-owned file aside (once) and record it; returns the backup path.

    A `.glance-bak` that is not ours (not in the manifest) is never adopted or overwritten:
    the backup gets a fresh numbered name instead.
    """
    data = _load(manifest)
    known = _recorded_backup(path, data)
    if known is not None:
        return known
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    n = 0
    while bak.exists():
        n += 1
        bak = path.with_name(f"{path.name}{BACKUP_SUFFIX}.{n}")
    shutil.copy2(path, bak)
    data["backups"].append({"id": path.stem, "backup": str(bak), "path": str(path)})
    _save(manifest, data)
    return bak


def _flagged(text: str) -> str:
    return "\n".join(rewrite_exec(line) for line in text.split("\n"))


def install_override(t: Target, user_dir: Path, manifest: Path) -> Path:
    """Write `user_dir/<id>.desktop`: the source with every Exec flagged and our marker."""
    _ensure_user_dir(user_dir, manifest)
    dest = user_dir / f"{t.id}.desktop"
    base = t.source
    if dest.exists():
        if _is_ours(_read(dest)):
            bak = _recorded_backup(dest, _load(manifest))
            if bak is not None and bak.exists():
                base = bak  # rebuilt over the user's original, not over our own output
        else:
            base = _backup(dest, manifest)
    src_hash = _sha(t.source.read_bytes())
    lines = _flagged(_read(base)).split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "[Desktop Entry]":
            eol = "\r" if line.endswith("\r") else ""
            lines[i + 1:i + 1] = [MARKER + eol, HASH_KEY + src_hash + eol]
            break
    else:
        lines = ["[Desktop Entry]", MARKER, HASH_KEY + src_hash, *lines]
    _atomic_write(dest, _enc("\n".join(lines)))
    return dest


def _is_ours(text: str) -> bool:
    return MARKER in text.splitlines()


def patch_user_launchers(user_dir: Path, manifest: Path) -> list[Path]:
    """Flag user-owned browser launchers (e.g. PWA shortcuts) in place, with backups."""
    patched: list[Path] = []
    for f in unpatched_launchers(user_dir):
        new = _flagged(_read(f))
        _backup(f, manifest)
        _atomic_write(f, _enc(new), f.stat().st_mode & 0o7777)
        patched.append(f)
    return patched


def unpatched_launchers(user_dir: Path) -> list[Path]:
    if not user_dir.is_dir():
        return []
    out = []
    for f in sorted(user_dir.glob("*.desktop")):
        text = _read(f)
        binary = _command_word(_entry(text).get("Exec", ""))
        if (not _is_ours(text) and os.path.basename(binary) in BROWSER_BINARIES
                and _flagged(text) != text):
            out.append(f)
    return out


def override_state(t: Target, user_dir: Path) -> str:
    """missing | current | stale | foreign."""
    f = user_dir / f"{t.id}.desktop"
    if not f.is_file():
        return "missing"
    text = _read(f)
    if _is_ours(text):
        recorded = next((ln[len(HASH_KEY):].strip() for ln in text.splitlines()
                         if ln.startswith(HASH_KEY)), "")
        try:
            return "current" if recorded == _sha(t.source.read_bytes()) else "stale"
        except OSError:
            return "current"
    return "current" if FLAG in text else "foreign"


def undo_all(user_dir: Path, manifest: Path) -> list[Path]:
    """Delete our override files, restore every backup byte-for-byte, drop the manifest.

    Manifest entries pointing outside `user_dir` are ignored: the manifest is never trusted
    to name files elsewhere.
    """
    data = _load(manifest)
    touched: list[Path] = []
    root = user_dir.resolve()
    restore: dict[Path, Path] = {}
    for b in data["backups"]:
        try:
            bak = Path(b["backup"])
            orig = Path(b.get("path") or str(b["backup"])[:-len(BACKUP_SUFFIX)])
            ok = bak.resolve().parent == root and orig.resolve().parent == root
        except (KeyError, TypeError, OSError, ValueError):
            ok = False
        if ok:
            restore[bak] = orig
        else:
            log_event("desktop_undo_skipped", reason="path_outside_user_dir")
    if user_dir.is_dir():
        for f in sorted(user_dir.glob("*.desktop")):
            if _is_ours(_read(f)) and f not in {o for b, o in restore.items() if b.exists()}:
                f.unlink()
                touched.append(f)
    for bak, original in restore.items():
        if bak.exists():
            os.replace(bak, original)
            touched.append(original)
    manifest.unlink(missing_ok=True)
    for key, d in (("created_manifest_dir", manifest.parent), ("created_user_dir", user_dir)):
        if data.get(key):
            try:
                d.rmdir()
            except OSError:
                pass
    return touched
