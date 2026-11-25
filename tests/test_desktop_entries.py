"""Reversible .desktop accessibility overrides: tmp dirs only, never the real applications dir."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from glance.doctor import desktop_entries as de
from glance.doctor.checks import CHECKS, DoctorEnv
from glance.doctor.desktop_entries import (
    FLAG,
    Target,
    find_targets,
    install_override,
    override_state,
    patch_user_launchers,
    rewrite_exec,
    undo_all,
)
from glance.doctor.runner import apply_fixes, run_checks
from glance.doctor.runner import undo_all as runner_undo_all

CHROME = """[Desktop Entry]
Name=Google Chrome
Exec=/usr/bin/google-chrome-stable %U
Icon=google-chrome
Actions=new-window;new-private-window;

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/google-chrome-stable

[Desktop Action new-private-window]
Name=New Incognito Window
Exec=/usr/bin/google-chrome-stable --incognito
"""
SLACK = "[Desktop Entry]\nName=Slack\nExec=/usr/bin/slack -s %U\nIcon=slack\n"


@pytest.mark.parametrize("line, want", [
    ("Exec=/usr/bin/google-chrome-stable %U", "Exec=/usr/bin/google-chrome-stable --force-renderer-accessibility %U"),
    ("Exec=/usr/bin/google-chrome-stable --incognito", "Exec=/usr/bin/google-chrome-stable --force-renderer-accessibility --incognito"),
    ("Exec=/usr/bin/discord --url -- %u", "Exec=/usr/bin/discord --force-renderer-accessibility --url -- %u"),
    ("Exec=env BAMF_DESKTOP_FILE_HINT=x /snap/bin/code %F", "Exec=env BAMF_DESKTOP_FILE_HINT=x /snap/bin/code --force-renderer-accessibility %F"),
    ('Exec="/opt/My App/app" %U', 'Exec="/opt/My App/app" --force-renderer-accessibility %U'),
    ("Exec=/usr/bin/slack", "Exec=/usr/bin/slack --force-renderer-accessibility"),
    ("Name=Slack", "Name=Slack")])
def test_rewrite_exec(line, want):
    assert rewrite_exec(line) == want and rewrite_exec(want) == want


@pytest.fixture
def dirs(tmp_path):
    sysdir, user = tmp_path / "sys", tmp_path / "home/.local/share/applications"
    sysdir.mkdir()
    user.mkdir(parents=True)
    (sysdir / "google-chrome.desktop").write_text(CHROME)
    (sysdir / "slack.desktop").write_text(SLACK)
    return sysdir, user, tmp_path / "data" / "desktop-overrides.json"


def target(sysdir, name, default=True):
    return Target(name, sysdir / f"{name}.desktop", name, f"/usr/bin/{name}", default)


def test_override_covers_all_exec_lines_and_marks_file(dirs):
    sysdir, user, manifest = dirs
    t = target(sysdir, "google-chrome")
    before = (sysdir / "google-chrome.desktop").read_bytes()
    out = install_override(t, user, manifest)
    text = out.read_text()
    assert out == user / "google-chrome.desktop"
    assert text.count(FLAG) == 3 and "X-Glance-Override=true" in text
    assert "X-Glance-Source-SHA256=" in text
    assert text.index("X-Glance-Override") < text.index("[Desktop Action")
    assert (sysdir / "google-chrome.desktop").read_bytes() == before


def test_existing_user_entry_is_backed_up_and_restored(dirs):
    sysdir, user, manifest = dirs
    mine = "[Desktop Entry]\nName=Slack (mine)\nExec=/usr/bin/slack --my-flag %U\n"
    (user / "slack.desktop").write_text(mine)
    install_override(target(sysdir, "slack"), user, manifest)
    got = (user / "slack.desktop").read_text()
    assert "--my-flag" in got and FLAG in got and "Slack (mine)" in got
    assert json.loads(manifest.read_text())["backups"][0]["id"] == "slack"
    undo_all(user, manifest)
    assert (user / "slack.desktop").read_text() == mine
    assert not (user / "slack.desktop.glance-bak").exists()


def test_undo_removes_only_glance_files_and_is_idempotent(dirs):
    sysdir, user, manifest = dirs
    (user / "kitty.desktop").write_text("[Desktop Entry]\nExec=kitty\n")
    install_override(target(sysdir, "google-chrome"), user, manifest)
    install_override(target(sysdir, "slack"), user, manifest)
    removed = undo_all(user, manifest)
    assert sorted(p.name for p in removed) == ["google-chrome.desktop", "slack.desktop"]
    assert sorted(p.name for p in user.iterdir()) == ["kitty.desktop"]
    assert not manifest.exists()
    assert undo_all(user, manifest) == []


def test_override_regenerated_when_source_changes(dirs):
    sysdir, user, manifest = dirs
    chrome = target(sysdir, "google-chrome")
    assert override_state(chrome, user) == "missing"
    install_override(chrome, user, manifest)
    assert override_state(chrome, user) == "current"
    chrome.source.write_text(chrome.source.read_text().replace("%U", "--new-flag %U"))
    assert override_state(chrome, user) == "stale"
    install_override(chrome, user, manifest)
    assert "--new-flag" in (user / "google-chrome.desktop").read_text()
    assert override_state(chrome, user) == "current"


def test_foreign_state_and_regeneration_keeps_original_backup(dirs):
    sysdir, user, manifest = dirs
    mine = "[Desktop Entry]\nName=Slack (mine)\nExec=/usr/bin/slack\n"
    (user / "slack.desktop").write_text(mine)
    slack = target(sysdir, "slack")
    assert override_state(slack, user) == "foreign"
    install_override(slack, user, manifest)
    install_override(slack, user, manifest)  # second run must not back up our own override
    undo_all(user, manifest)
    assert (user / "slack.desktop").read_text() == mine


def test_pwa_launcher_patched_in_place_and_restored(dirs):
    _, user, manifest = dirs
    pwa = ("[Desktop Entry]\nName=Docs\nExec=/opt/google/chrome/google-chrome "
           "--profile-directory=Default --app-id=abc\nStartupWMClass=crx_abc\n")
    (user / "chrome-abc-Default.desktop").write_text(pwa)
    (user / "kitty.desktop").write_text("[Desktop Entry]\nExec=kitty\n")
    patched = patch_user_launchers(user, manifest)
    assert [p.name for p in patched] == ["chrome-abc-Default.desktop"]
    assert FLAG in (user / "chrome-abc-Default.desktop").read_text()
    assert patch_user_launchers(user, manifest) == []  # idempotent
    undo_all(user, manifest)
    assert (user / "chrome-abc-Default.desktop").read_text() == pwa
    assert sorted(p.name for p in user.iterdir()) == ["chrome-abc-Default.desktop", "kitty.desktop"]


def test_find_targets_defaults_and_detected(dirs):
    sysdir, user, _ = dirs
    app = sysdir.parent / "opt/obsidian"
    (app / "resources").mkdir(parents=True)
    (app / "resources/app.asar").write_bytes(b"x")
    (app / "obsidian").write_text("#!/bin/sh\n")
    (sysdir / "obsidian.desktop").write_text(f"[Desktop Entry]\nName=Obsidian\nExec={app}/obsidian %U\n")
    (sysdir / "gedit.desktop").write_text("[Desktop Entry]\nName=gedit\nExec=/usr/bin/gedit %U\n")
    got = {(t.id, t.default) for t in find_targets([sysdir], user)}
    assert got == {("google-chrome", True), ("slack", True), ("obsidian", False)}


def make_env(home: Path, sysdir: Path) -> DoctorEnv:
    return DoctorEnv(home=home, environ={"XDG_DATA_DIRS": str(sysdir.parent)}, run=lambda a: (0, ""),
                     procs=list, ctl=lambda t, p: None, which=lambda n: None,
                     healths=dict)


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_doctor_fix_then_undo_round_trip(dirs):
    sysdir, user, _ = dirs
    (sysdir.parent / "applications").mkdir()
    for f in sysdir.iterdir():
        f.rename(sysdir.parent / "applications" / f.name)
    sysdir.rmdir()
    home = user.parents[2]
    (user / "slack.desktop").write_text("[Desktop Entry]\nName=mine\nExec=/usr/bin/slack\n")
    start = tree(home)
    env = make_env(home, sysdir)
    check = next(c for c in CHECKS if c.id == "desktop_flags")
    ids = lambda rs: {r.id: r for r in rs}
    assert ids(run_checks(env, [check]))["desktop_flags"].status == "fail"
    assert ids(apply_fixes(env, [check], apps=[]))["desktop_flags"].status == "ok"
    assert FLAG in (user / "google-chrome.desktop").read_text()
    runner_undo_all(env, [check])
    assert ids(run_checks(env, [check]))["desktop_flags"].status == "fail"
    assert tree(home) == start


def test_no_update_desktop_database_artifacts(dirs):
    sysdir, user, manifest = dirs
    install_override(target(sysdir, "slack"), user, manifest)
    assert not (user / "mimeinfo.cache").exists() and not hasattr(de, "update_desktop_database")


def test_opt_in_unknown_app_returns_false_known_detected_true(dirs):
    sysdir, user, _ = dirs
    app = sysdir.parent / "opt/obsidian"
    (app / "resources").mkdir(parents=True)
    (app / "resources/app.asar").write_bytes(b"x")
    (app / "obsidian").write_text("x")
    (sysdir / "obsidian.desktop").write_text(f"[Desktop Entry]\nName=O\nExec={app}/obsidian %U\n")
    share = sysdir.parent / "share"
    (share / "applications").mkdir(parents=True)
    (share / "applications/obsidian.desktop").write_text((sysdir / "obsidian.desktop").read_text())
    env = make_env(user.parents[2], share / "applications")
    assert env.override_app("nonesuch") is False and not (user / "nonesuch.desktop").exists()
    assert env.override_app("obsidian") is True and FLAG in (user / "obsidian.desktop").read_text()


def test_undo_ignores_manifest_entries_outside_user_dir(dirs, tmp_path):
    sysdir, user, manifest = dirs
    victim = tmp_path / "elsewhere.desktop"
    victim.write_text("precious")
    evil = tmp_path / "evil.glance-bak"
    evil.write_text("overwrite me in")
    (user / "slack.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/slack\n")
    install_override(target(sysdir, "slack"), user, manifest)
    data = json.loads(manifest.read_text())
    data["backups"].append({"id": "x", "backup": str(evil), "path": str(victim)})
    manifest.write_text(json.dumps(data))
    undo_all(user, manifest)
    assert victim.read_text() == "precious" and evil.exists()
    assert (user / "slack.desktop").read_text() == "[Desktop Entry]\nExec=/usr/bin/slack\n"


def test_foreign_glance_bak_is_never_adopted_or_overwritten(dirs):
    sysdir, user, manifest = dirs
    mine = "[Desktop Entry]\nName=mine\nExec=/usr/bin/slack\n"
    (user / "slack.desktop").write_text(mine)
    (user / "slack.desktop.glance-bak").write_text("someone else's file")
    install_override(target(sysdir, "slack"), user, manifest)
    assert (user / "slack.desktop.glance-bak").read_text() == "someone else's file"
    undo_all(user, manifest)
    assert (user / "slack.desktop").read_text() == mine
    assert (user / "slack.desktop.glance-bak").read_text() == "someone else's file"
    assert sorted(p.name for p in user.iterdir() if p.name.startswith("slack")) == [
        "slack.desktop", "slack.desktop.glance-bak"]


def test_non_utf8_and_crlf_launchers_survive_byte_exact(dirs):
    _sysdir, user, manifest = dirs
    pwa = (b"[Desktop Entry]\r\nName=Caf\xe9 \xff\r\nExec=/opt/google/chrome/google-chrome "
           b"--app-id=abc\r\n")
    f = user / "chrome-abc-Default.desktop"
    f.write_bytes(pwa)
    patch_user_launchers(user, manifest)
    got = f.read_bytes()
    assert b"Caf\xe9 \xff\r\n" in got and FLAG.encode() in got and b"\r\n" in got
    assert got.count(b"\n") == got.count(b"\r\n")
    undo_all(user, manifest)
    assert f.read_bytes() == pwa


def test_undo_removes_manifest_dir_it_created(dirs):
    sysdir, user, manifest = dirs
    (user / "slack.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/slack\n")
    install_override(target(sysdir, "slack"), user, manifest)
    assert manifest.parent.is_dir()
    undo_all(user, manifest)
    assert not manifest.parent.exists()


def test_override_with_missing_backup_is_still_removed(dirs):
    sysdir, user, manifest = dirs
    (user / "slack.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/slack\n")
    install_override(target(sysdir, "slack"), user, manifest)
    (user / "slack.desktop.glance-bak").unlink()
    undo_all(user, manifest)
    assert not (user / "slack.desktop").exists()


def test_non_utf8_name_is_printable_and_json_safe(dirs):
    from glance.doctor.runner import render, to_json
    sysdir, user, _ = dirs
    (sysdir / "discord.desktop").write_bytes(b"[Desktop Entry]\nName=Disc\xe9rd\nExec=/usr/bin/discord\n")
    assert all(t.name.encode("utf-8") for t in find_targets([sysdir], user))
    (sysdir.parent / "applications").mkdir()
    for f in sysdir.iterdir():
        (sysdir.parent / "applications" / f.name).write_bytes(f.read_bytes())
    results = run_checks(make_env(user.parents[2], sysdir), [next(c for c in CHECKS if c.id == "desktop_flags")])
    render(results).encode("utf-8")
    json.dumps(to_json(results)).encode("utf-8")
