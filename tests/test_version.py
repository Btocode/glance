"""One version, from pyproject.toml: `glance --version`, the comparisons the update check uses."""
import re
from pathlib import Path

import pytest

from glance import cli, version

REPO = Path(__file__).resolve().parents[1]


def pyproject_version():
    return re.search(r'^version = "([^"]+)"$', (REPO / "pyproject.toml").read_text(), re.MULTILINE)[1]


def test_version_is_pyprojects():
    assert version.version() == pyproject_version()
    assert version.__version__ == pyproject_version()


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"Glance {pyproject_version()}"


def test_from_pyproject_only_reads_glances_project_table(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[tool.x]\nversion = "9.9.9"\n[project]\nname = "glance"\nversion = "1.2.3"\n')
    assert version.from_pyproject(p) == "1.2.3"
    p.write_text('[project]\nname = "other"\nversion = "1.2.3"\n')
    assert version.from_pyproject(p) is None
    assert version.from_pyproject(tmp_path / "missing.toml") is None


@pytest.mark.parametrize("newer, older", [
    ("0.5.0", "0.4.0"), ("v0.10.0", "0.9.9"), ("1.0", "0.99.0"), ("0.5.0", "0.5.0rc1"),
    ("0.5.0rc2", "0.5.0rc1"), ("0.4.1", "0.4.0"),
])
def test_is_newer(newer, older):
    assert version.is_newer(newer, older) and not version.is_newer(older, newer)


def test_equal_versions_are_not_newer():
    assert not version.is_newer("v0.4.0", "0.4.0")


def test_changelog_has_this_version():
    text = (REPO / "CHANGELOG.md").read_text()
    assert f"## [{pyproject_version()}] - " in text
