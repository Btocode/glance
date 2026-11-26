"""glance setup: finds a package's support tree and runs the installer's per-user mode
(fake paths only; nothing is run)."""
from pathlib import Path

from glance import cli, setup

REPO = Path(__file__).resolve().parents[1]


def make_tree(d: Path) -> Path:
    for f in ("install.sh", "uninstall.sh", "pyproject.toml", "scripts/fetch-harper.sh",
              "scripts/install-macos.sh", "scripts/uninstall-macos.sh",
              "packaging/glance.service"):
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        (d / f).write_text("")
    return d


def make_bin(d: Path) -> Path:
    d.mkdir(parents=True)
    for n in ("glance", "glanced", "python"):
        (d / n).write_text("")
    return d


def test_the_checkout_is_a_tree():
    assert setup.is_tree(REPO)


def test_deb_layout(tmp_path):
    tree = make_tree(tmp_path / "opt/glance")
    venv_bin = make_bin(tmp_path / "opt/glance/.venv/bin")
    runs = []
    rc = setup.main(run=lambda argv: runs.append(argv) or 0, environ={},
                    prefix=venv_bin.parent, package_dir=tmp_path / "site/glance",
                    executable=str(venv_bin / "python"), platform="linux")
    assert rc == 0
    assert runs == [["bash", str(tree / "install.sh"), "--setup-only", "--app-bin",
                     str(venv_bin)]]


def test_aur_layout_and_no_start(tmp_path):
    usr = tmp_path / "usr"
    tree = make_tree(usr / "share/glance")
    make_bin(usr / "bin")
    runs = []
    setup.main(no_start=True, run=lambda argv: runs.append(argv) or 0, environ={}, prefix=usr,
               package_dir=tmp_path / "usr/lib/python3/site-packages/glance",
               executable=str(usr / "bin/python"), platform="linux")
    assert runs == [["bash", str(tree / "install.sh"), "--setup-only", "--app-bin",
                     str(usr / "bin"), "--no-start"]]


def test_homebrew_layout_runs_the_macos_script(tmp_path):
    cellar = tmp_path / "Cellar/glance/0.4.0"
    tree = make_tree(cellar / "share/glance")
    libexec_bin = make_bin(cellar / "libexec/bin")
    runs = []
    setup.main(run=lambda argv: runs.append(argv) or 0, environ={}, prefix=cellar / "libexec",
               package_dir=tmp_path / "x/glance", executable=str(libexec_bin / "python"),
               platform="darwin")
    assert runs[0][:3] == ["bash", str(tree / "scripts/install-macos.sh"), "--setup-only"]


def test_undo_runs_the_uninstaller(tmp_path):
    tree = make_tree(tmp_path / "share")
    runs = []
    setup.main(undo=True, run=lambda argv: runs.append(argv) or 0,
               environ={"GLANCE_SHARE": str(tree)}, prefix=tmp_path / "none",
               package_dir=tmp_path / "x/glance", platform="linux")
    assert runs == [["bash", str(tree / "uninstall.sh")]]
    runs.clear()
    setup.main(undo=True, run=lambda argv: runs.append(argv) or 0,
               environ={"GLANCE_SHARE": str(tree)}, prefix=tmp_path / "none",
               package_dir=tmp_path / "x/glance", platform="darwin")
    assert runs == [["bash", str(tree / "scripts/uninstall-macos.sh")]]


def test_no_tree_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(setup, "candidates", lambda env, prefix, pkg: [tmp_path / "nothing"])
    assert setup.main(run=lambda argv: 0) == 1
    assert "GLANCE_SHARE" in capsys.readouterr().err


def test_cli_wires_setup(monkeypatch):
    seen = []
    monkeypatch.setattr(setup, "main", lambda **kw: seen.append(kw) or 0)
    assert cli.main(["setup", "--no-start"]) == 0
    assert cli.main(["setup", "--undo"]) == 0
    assert seen == [{"undo": False, "no_start": True}, {"undo": True, "no_start": False}]
