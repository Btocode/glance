"""Release workflow and packaging recipes: cheap consistency checks (nothing is built or
published; no network)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packaging"


def sh(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def version() -> str:
    return re.search(r'^version = "([^"]+)"$', (REPO / "pyproject.toml").read_text(), re.MULTILINE)[1]


def core_pins() -> dict[str, str]:
    out = {}
    for line in (REPO / "scripts/core-wheels.sha256").read_text().splitlines():
        if line and not line.startswith("#"):
            sha, name = line.split("  ")
            out[name] = sha
    return out


# ------------------------------------------------------------------ release workflow

def test_release_workflow_runs_the_suite_then_publishes():
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    on = wf.get("on") or wf.get(True)  # YAML 1.1 reads a bare `on` as True
    assert on["push"]["tags"] == ["v[0-9]*"]  # never core-v*
    jobs = wf["jobs"]
    assert jobs["tests"]["uses"] == "./.github/workflows/tests.yml"
    assert jobs["build"]["needs"] == "tests" and jobs["publish"]["needs"] == "build"
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    steps = "\n".join(s.get("run", "") for s in jobs["build"]["steps"] + jobs["publish"]["steps"])
    for want in ("scripts/changelog-section.sh", "uv build", "SHA256SUMS", "gh release create",
                 "--notes-file release-notes.md"):
        assert want in steps
    # the reusable suite: callable, and its concurrency group is not the release's
    tests = yaml.safe_load((REPO / ".github/workflows/tests.yml").read_text())
    assert "workflow_call" in (tests.get("on") or tests.get(True))
    assert wf["concurrency"]["group"] != tests["concurrency"]["group"].replace(
        "${{ github.workflow }}", "release")


def test_core_wheels_workflow_is_separate():
    wf = yaml.safe_load((REPO / ".github/workflows/core-wheels.yml").read_text())
    assert (wf.get("on") or wf.get(True))["push"]["tags"] == ["core-v*"]


def test_changelog_section_for_this_version():
    out = sh("bash", str(REPO / "scripts/changelog-section.sh"), f"v{version()}").stdout
    assert out.strip() and "## [" not in out and "### Added" in out
    bad = sh("bash", str(REPO / "scripts/changelog-section.sh"), "99.0.0")
    assert bad.returncode != 0


def test_changelog_releases_are_in_order_with_dates():
    heads = re.findall(r"^## \[(\d[^\]]*)\] - (\d{4}-\d\d-\d\d)$",
                       (REPO / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert [v for v, _ in heads] == ["0.4.0", "0.3.0", "0.2.0", "0.1.0"]
    assert heads[0][0] == version()


# ------------------------------------------------------------------ AUR

def _pkgbuild_var(text: str, name: str) -> str:
    return re.search(rf"^{name}=(\S+)$", text, re.MULTILINE)[1].strip("'\"")


def test_pkgbuild_matches_the_release_and_the_pins():
    text = (PKG / "aur/PKGBUILD").read_text()
    assert _pkgbuild_var(text, "pkgname") == "glance-writing-assistant"
    assert _pkgbuild_var(text, "pkgver") == version()
    corever = _pkgbuild_var(text, "_corever")
    wheel = f"glance_core-{corever}-cp310-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl"
    assert core_pins()[wheel] in text
    for dep in ("python-gobject", "gtk3", "at-spi2-core", "libwnck3", "python-xlib",
                "python-httpx", "python-tomlkit"):
        assert f"'{dep}'" in text
    assert "/usr/share/glance" in text and "_packaged" in text
    assert sh("bash", "-n", str(PKG / "aur/PKGBUILD")).returncode == 0


def test_srcinfo_agrees_with_pkgbuild():
    text = (PKG / "aur/PKGBUILD").read_text()
    info = (PKG / "aur/.SRCINFO").read_text()
    fields = re.findall(r"^\s*(\w+) = (.*)$", info, re.MULTILINE)
    get = lambda k: [v for f, v in fields if f == k]
    assert get("pkgver") == [version()] and get("pkgname") == ["glance-writing-assistant"]
    deps = re.search(r"depends=\((.*?)\)", text, re.DOTALL)[1]
    assert get("depends") == re.findall(r"'([^']+)'", deps)
    assert get("sha256sums")[1] == core_pins()[get("noextract")[0]]
    assert all(f"v{version()}" in s or "core-v" in s for s in get("source"))


# ------------------------------------------------------------------ Homebrew

def test_formula_matches_the_release_and_the_pins():
    text = (PKG / "homebrew/glance.rb").read_text()
    assert text.count("class Glance < Formula") == 1
    assert f'url "https://github.com/Btocode/glance/releases/download/v{version()}/' \
           f'glance-{version()}.tar.gz"' in text
    pins = core_pins()
    for name, sha in pins.items():
        if "macosx" in name:
            assert name in text and sha in text
    assert "glance setup" in text and "Accessibility" in text and "_packaged" in text
    opens = len(re.findall(r"\b(do|class|def)\b(?!\w)", text))
    assert opens == len(re.findall(r"^\s*end$", text, re.MULTILINE))
    if shutil.which("ruby"):
        assert sh("ruby", "-c", str(PKG / "homebrew/glance.rb")).returncode == 0


# ------------------------------------------------------------------ Debian

def test_nfpm_config_and_maintainer_scripts():
    cfg = yaml.safe_load((PKG / "debian/nfpm.yaml").read_text())
    assert cfg["name"] == "glance" and cfg["version"] == "${VERSION}"
    assert cfg["arch"] == "amd64"
    assert {c["dst"] for c in cfg["contents"]} >= {"/opt/glance"}
    assert any(d.startswith("python3-venv") for d in cfg["depends"])
    for script in ("postinst", "prerm", "build-deb.sh"):
        path = PKG / "debian" / script
        assert path.stat().st_mode & 0o111, f"{script} is not executable"
        shell = "bash" if script.endswith(".sh") else "sh"
        assert sh(shell, "-n", str(path)).returncode == 0
    post = (PKG / "debian/postinst").read_text()
    assert "--no-index" in post and "glance setup" in post and "/usr/bin/" in post


@pytest.mark.parametrize("script", ["packaging/debian/build-deb.sh", "packaging/debian/postinst",
                                    "packaging/debian/prerm", "scripts/changelog-section.sh",
                                    "scripts/make-demo.sh"])
def test_scripts_are_shellcheck_clean(script):
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")
    p = sh("shellcheck", str(REPO / script))
    assert p.returncode == 0, p.stdout


def test_readme_demo_gif_is_there_and_small():
    gif = REPO / "docs/assets/demo.gif"
    assert gif.read_bytes()[:6] in (b"GIF89a", b"GIF87a")
    assert gif.stat().st_size < 3_000_000
    assert "docs/assets/demo.gif" in (REPO / "README.md").read_text()
