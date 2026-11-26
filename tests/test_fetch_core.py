"""scripts/fetch-core.sh: a pinned glance_core wheel or nothing (harper-ls stays the engine)."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fetch-core.sh"
WHEEL = "glance_core-0.2.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"


def _exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


@pytest.fixture
def sb(tmp_path):
    log = tmp_path / "log"
    log.write_text("")
    marker = tmp_path / "installed"
    # a venv python: `import glance_core` works once the stub uv "installed" it
    py = _exe(tmp_path / "python", f'echo "python $*" >> "{log}"\n'
              f'[ -f "{marker}" ] || exit 1\necho 0.2.0\n')
    uv = _exe(tmp_path / "uv", f'echo "uv $*" >> "{log}"\ntouch "{marker}"\n')
    release = tmp_path / "release" / "core-v0.2.0"
    release.mkdir(parents=True)
    (release / WHEEL).write_bytes(b"wheel bytes")
    pins = tmp_path / "pins"
    pins.write_text("# header\n")

    def run(*args, pin_sha=None, platform="Linux-x86_64"):
        if pin_sha is not None:
            pins.write_text(f"# header\n{pin_sha}  {WHEEL}\n")
        env = {"PATH": "/usr/bin:/bin", "GLANCE_CORE_PINS": str(pins),
               "GLANCE_CORE_BASE_URL": f"file://{tmp_path / 'release'}",
               "GLANCE_FETCH_PLATFORM": platform, "GLANCE_UV": str(uv)}
        p = subprocess.run(["bash", str(SCRIPT), str(py), *args], env=env, text=True,
                           capture_output=True, check=False, timeout=60)
        return p, [ln for ln in log.read_text().splitlines() if ln.startswith("uv")]

    run.good_sha = hashlib.sha256(b"wheel bytes").hexdigest()
    return run


def test_no_pin_keeps_harper_ls(sb):
    p, uv = sb()
    assert p.returncode == 0 and "harper-ls" in p.stdout and uv == []


def test_pinned_wheel_is_verified_and_installed(sb):
    p, uv = sb(pin_sha=sb.good_sha)
    assert p.returncode == 0, p.stderr
    assert len(uv) == 1 and uv[0].endswith(WHEEL) and "--python" in uv[0]
    p, uv = sb(pin_sha=sb.good_sha)  # rerun: the same version already imports
    assert p.returncode == 0 and "already installed" in p.stdout and len(uv) == 1


def test_checksum_mismatch_installs_nothing(sb):
    p, uv = sb(pin_sha="0" * 64)
    assert p.returncode == 1 and "checksum mismatch" in p.stderr and uv == []


def test_other_platforms_have_no_wheel_and_mac_pins_do_not_match_linux(sb):
    p, uv = sb(pin_sha=sb.good_sha, platform="Darwin-arm64")
    assert p.returncode == 0 and uv == [] and "harper-ls" in p.stdout
    p, uv = sb(platform="FreeBSD-amd64")
    assert p.returncode == 0 and uv == []


@pytest.mark.skipif(shutil.which("cargo", path="/usr/bin:/bin") is not None,
                    reason="cargo is on the system PATH")
def test_with_rust_needs_cargo(sb):
    p, uv = sb("--with-rust")  # PATH has no cargo
    assert p.returncode == 1 and "cargo" in p.stderr and uv == []


def test_pins_file_is_well_formed():
    for line in (REPO / "scripts" / "core-wheels.sha256").read_text().splitlines():
        if line and not line.startswith("#"):
            sha, _, name = line.partition("  ")
            assert len(sha) == 64 and name.startswith("glance_core-") and name.endswith(".whl")


def test_script_lints():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    if shutil.which("shellcheck"):
        p = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True,
                           check=False)
        assert p.returncode == 0, p.stdout
