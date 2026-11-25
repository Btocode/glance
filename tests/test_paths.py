import stat

from glance.paths import capture_sock, ctl_sock, runtime_dir


def test_runtime_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700
    assert capture_sock() == tmp_path / "glance" / "capture.sock"
    assert ctl_sock() == tmp_path / "glance" / "ctl.sock"
