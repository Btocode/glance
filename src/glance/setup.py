"""`glance setup`: the per-user half of an install, for Glance installed by a package.

A package (AUR, .deb, Homebrew) puts Glance's commands and its support tree (install.sh,
uninstall.sh, scripts/, packaging/, extension/) on the system. What belongs to one user
cannot go there: the systemd user unit or LaunchAgent, the GNOME Shell extension in
~/.local/share, harper-ls in ~/.local/share/glance/bin, and what `glance doctor --fix`
sets up. `glance setup` runs the installer's own `--setup-only` mode for exactly that, so a
package install and a curl | bash install leave the same per-user files, recorded in the same
manifest; `glance setup --undo` runs uninstall.sh, which reverses them."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

Runner = Callable[[Sequence[str]], int]


def is_tree(d: Path) -> bool:
    """A Glance support tree: what install.sh --setup-only needs next to it."""
    return all((d / f).is_file() for f in (
        "install.sh", "uninstall.sh", "pyproject.toml", "scripts/fetch-harper.sh",
        "packaging/glance.service"))


def candidates(environ: Mapping[str, str], prefix: Path, package_dir: Path) -> list[Path]:
    """Where a package may have put the tree, most specific first."""
    out = []
    if environ.get("GLANCE_SHARE"):
        out.append(Path(environ["GLANCE_SHARE"]))
    out += [
        package_dir.parents[1],                 # a checkout (src/glance -> the repository)
        prefix.parent,                          # .deb: /opt/glance/.venv -> /opt/glance
        prefix.parent / "share" / "glance",     # Homebrew: <cellar>/libexec -> <cellar>/share
        prefix / "share" / "glance",            # /usr (AUR), /usr/local
        Path("/usr/share/glance"),
        Path("/usr/local/share/glance"),
    ]
    return out


def find_tree(environ: Mapping[str, str] | None = None, prefix: Path | None = None,
              package_dir: Path | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    for d in candidates(env, prefix or Path(sys.prefix),
                        package_dir or Path(__file__).resolve().parent):
        if is_tree(d):
            return d
    return None


def app_bin(executable: str | None = None,
            which: Callable[[str], str | None] = shutil.which) -> Path | None:
    """The directory holding this Glance's `glance` and `glanced`: next to the running
    Python (a venv's bin, or /usr/bin), else wherever glanced is on PATH."""
    here = Path(executable or sys.executable).parent
    if (here / "glanced").exists() and (here / "glance").exists():
        return here
    found = which("glanced")
    return Path(found).parent if found else None


def setup_argv(tree: Path, bin_dir: Path, platform: str, no_start: bool) -> list[str]:
    script = tree / ("scripts/install-macos.sh" if platform == "darwin" else "install.sh")
    argv = ["bash", str(script), "--setup-only", "--app-bin", str(bin_dir)]
    if no_start:
        argv.append("--no-start")
    return argv


def undo_argv(tree: Path, platform: str) -> list[str]:
    script = tree / ("scripts/uninstall-macos.sh" if platform == "darwin" else "uninstall.sh")
    return ["bash", str(script)]


def _run(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError as e:
        print(f"glance setup: could not run {argv[1]}: {e}", file=sys.stderr)
        return 1


def main(undo: bool = False, no_start: bool = False, run: Runner = _run,
         environ: Mapping[str, str] | None = None, prefix: Path | None = None,
         package_dir: Path | None = None, executable: str | None = None,
         platform: str = sys.platform) -> int:
    tree = find_tree(environ, prefix, package_dir)
    if tree is None:
        print("glance setup: Glance's support files (install.sh, scripts/, packaging/) were not "
              "found. Set GLANCE_SHARE to the directory your package put them in, or install "
              "with the curl one-liner instead.", file=sys.stderr)
        return 1
    if undo:
        return run(undo_argv(tree, platform))
    bin_dir = app_bin(executable)
    if bin_dir is None:
        print("glance setup: glanced was not found next to this Python or on PATH",
              file=sys.stderr)
        return 1
    return run(setup_argv(tree, bin_dir, platform, no_start))
