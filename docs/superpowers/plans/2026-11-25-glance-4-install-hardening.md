# Glance Plan 4 — Install, Doctor, Hardening and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Glance installable with one script, self-diagnosing (`glance doctor`), resilient to the failures in spec §8.6, and proven against the spec §8 acceptance criteria with automated matrices on fixture pages and a scripted manual smoke checklist for live apps.

**Architecture:** `glance doctor` is a list of small check objects, each with an optional fix and undo; the installer is a thin shell script that builds the venv and then delegates everything reversible to `glance doctor --fix`, so uninstall is `glance doctor --undo` plus file removal. Acceptance tests drive real browsers through XTEST against local fixture pages that report their own ground-truth text to a test HTTP server, so capture and write-back are compared against the editor's truth rather than against AT-SPI.

**Tech Stack:** Plans 1–3 stack; systemd user units; bash; `esbuild` (via the existing nvm Node 24, build-time only) to bundle ProseMirror / Slate / Lexical / Quill fixture pages.

**Spec:** `docs/superpowers/specs/2026-11-25-glance-design.md` (§5 decisions 1 and 6, §6 Processes, §8 all). Spike facts: S2.3, S2.8, S3.x in `docs/superpowers/spike/findings.md`.

**Plan sequence:** 0 spike → 1 foundation → 2 backends/tone → 3 features → **4 install/hardening (this)**. Uses unchanged: `glance wrapper install|uninstall|status` and `claude_wrapper.status/install/uninstall` (Plan 3), `BackendSupervisor.healths`, `CONNECT`, `ChildSupervisor`, `Controller.status()`, `load_config`, `harper_bin()`, `find_secrets`, `tests/desktop/gtk_app.py`, the `GLANCE_TEST` / `GLANCE_FAKE_BACKEND` test switches.

## Global Constraints

- All earlier Global Constraints still apply.
- Target is this PC first: Ubuntu 22.04, GNOME 42, X11, system Python 3.10. No distro/packaging matrix. Code stays configurable (no hard-coded home paths outside `paths.py`).
- `.desktop` overrides add `--force-renderer-accessibility` for Chrome, Brave, Slack, Discord, written to `~/.local/share/applications/`, and are fully reversible by `glance doctor --undo` and by uninstall. System files under `/usr` and `/var/lib/snapd` are never modified.
- `$EDITOR` is never changed globally; uninstall removes the `claude` wrapper block and leaves `.zshrc` otherwise byte-identical.
- systemd user unit bound to `graphical-session.target`, `Restart=on-failure`; restart after a crash ≤ 2 s.
- The installer runs `sudo` only when invoked with `--with-apt`, and only for `apt-get install -y gir1.2-ayatanaappindicator3-0.1`.
- Spec §8 numbers are **hard gates on fixtures** and **targets on live apps**. Live apps (Slack, Discord, Gmail, claude.ai, ChatGPT, Claude Code) are tested by hand in the user's real profiles, unsent drafts only, never sending.
- Automated browser tests use throwaway profiles, never the user's. Marker `apps` = needs Chrome/Brave/Firefox + a live X session; `paid` = makes real AI calls (run only with the user's go-ahead). Neither runs by default.
- Gates: capture ≥ 99 % of 200 scripted edits per app; write-back zero silent corruption across 500 applies per app; local fixes ≤ 150 ms after pause; chip reposition ≤ 50 ms; pre-generated apply ≤ 200 ms; analyze p50 ≤ 3.5 s CLI / ≤ 1.2 s API; first rewrite token ≤ 3.5 s CLI / ≤ 1 s API; live autocomplete ≤ 1.2 s p50 on `api`; zero stolen Tab keystrokes; idle CPU < 0.5 %; < 3 % of one core while typing; < 250 MB RSS excluding backend workers; secret redaction recall ≥ 99 %.

## Review Focus

1. The user already has their own `~/.local/share/applications/slack.desktop`, or a `claude` alias → install must not clobber it, and uninstall must restore the exact previous bytes (Task 2, `test_existing_user_entry_is_backed_up_and_restored`; Task 3, `test_install_reports_wrapper_conflict_without_writing`).
2. Slack/Chrome was already running without the flag when Glance was installed → doctor must say "restart Slack", not green (Task 1, `test_running_app_without_flag_is_warn_restart`).
3. A package update changes the system `.desktop` file (new `Exec`, new actions) → the stale override is detected and regenerated (Task 2, `test_override_regenerated_when_source_changes`).
4. Uninstall run twice, or after a partial install → succeeds, leaves no Glance files, touches nothing else (Task 3, `test_uninstall_is_idempotent_and_safe_on_partial_install`).
5. The unit starts in a Wayland session or before `DISPLAY` exists → one clear message and no restart loop (Task 3, `test_unsupported_session_exits_78`).

## File Structure

```
src/glance/doctor/__init__.py  checks.py  desktop_entries.py  runner.py
src/glance/daemon/session.py            suspend/resume, lock, monitor hotplug adapter
packaging/glance.service
install.sh  uninstall.sh
scripts/build-fixtures.sh  scripts/gen-secret-corpus.py
tests/acceptance/
  harness.py                 FixtureServer, Browser, Typist, edit_script
  fixtures/{textarea,contenteditable,prosemirror,slate,lexical,quill}.html
  fixtures/src/*.js  fixtures/package.json  fixtures/dist/*.js   (dist committed)
  corpus/secrets.jsonl  corpus/benign.jsonl
  test_capture_matrix.py  test_writeback_matrix.py  test_privacy_e2e.py
  test_redaction_corpus.py  test_disk_audit.py  test_resilience.py
  test_latency.py  test_resources.py
docs/acceptance/smoke-checklist.md  docs/acceptance/report.md
```

---

### Task 1: `glance doctor` checks

**Files:**
- Create: `src/glance/doctor/__init__.py`, `checks.py`, `runner.py`
- Modify: `src/glance/cli.py` (`glance doctor [--fix|--undo] [--app ID]... [--json]`), `src/glance/daemon/main.py` (append a `hotkeys_grabbed` provider to `controller.extra_status`)
- Test: `tests/test_doctor.py`

**Interfaces:**
- Produces (`checks.py`):
  - `@dataclass(frozen=True) CheckResult(id:str, title:str, status:str, detail:str, hint:str|None=None)` — status ∈ `ok|warn|fail`.
  - `class Check(Protocol): id:str; run(env:DoctorEnv) -> CheckResult; fix(env) -> bool` (default no-op False) `; undo(env) -> bool` (default False).
  - `@dataclass DoctorEnv(home:Path, environ:Mapping[str,str], run:Callable[[list[str]],tuple[int,str]], procs:Callable[[],list[tuple[int,list[str]]]], ctl:Callable[[str,dict],dict|None], which:Callable[[str],str|None])` — everything a check touches, so tests fake it.
  - Checks, in this order (id → fail/warn condition → hint):
    1. `session` — `XDG_SESSION_TYPE != "x11"` or no `DISPLAY` → fail "Glance needs an X11 session (log out, choose 'Ubuntu on Xorg')".
    2. `a11y` — `gsettings get org.gnome.desktop.interface toolkit-accessibility` not `true` → fail; fix: `gsettings set … true`.
    3. `python` — `/usr/bin/python3 -c "import gi; gi.require_version('Atspi','2.0'); …"` for Gtk 3, Atspi, Wnck, Notify, Secret, Xlib fails → fail with the missing module.
    4. `harper` — `harper_bin()` missing or not executable → fail; fix: run `scripts/fetch-harper.sh`.
    5. `tray` — `AyatanaAppIndicator3` GIR missing → warn, hint `sudo apt install gir1.2-ayatanaappindicator3-0.1`.
    6. `config` — `load_config` raises → fail with key and reason.
    7. `daemon` — `ctl("status")` is `None` → fail "glanced is not running", hint `systemctl --user start glance`; `capture_connected` false → fail; `harper_alive` false → warn.
    8. `hotkeys` — any configured hotkey not in `status()["hotkeys_grabbed"]` → warn naming the chord ("another app owns it; change it in config.toml").
    9. `desktop_flags` — delegates to `desktop_entries` (Task 2): a default target without an override → fail, fixable; override stale → warn, fixable.
    10. `running_flags` — for each target app with a running process (`procs()`), main process argv lacks `--force-renderer-accessibility` → warn "Restart <App> to enable Glance there".
    11. `firefox` — per findings S3: ok, or warn with the recorded limitation.
    12. `other_electron` — detected Chromium/Electron apps outside the default four → warn listing ids, hint `glance doctor --fix --app <id>` (opt-in only).
    13. `backend` — no backend `ready` or none chosen → warn "No AI connected" + the four `CONNECT` commands + `glance backend use <name>`; local grammar noted as working.
    14. `wrapper` — `claude_wrapper.status` `absent` → warn, fixable; `conflict` → warn with the manual snippet; also fail if the *shell's* `$EDITOR`/`$VISUAL` (from `zsh -ic 'print -r -- $EDITOR $VISUAL'`) contains `glance-edit`.
    15. `autostart` — `systemctl --user is-enabled glance.service` not `enabled` → warn, fixable.
    16. `clipboard_history` — a clipboard-history extension is enabled (`gnome-extensions list --enabled` matches `clipboard`) → ok with detail "long fixes are pasted with the password-manager hint; short fixes are typed".
- Produces (`runner.py`): `run_checks(env, checks) -> list[CheckResult]`; `apply_fixes(env, checks, apps:list[str]) -> list[CheckResult]` (runs `fix` for every non-ok check, then re-runs); `undo_all(env, checks) -> None`; `render(results) -> str` (one line per check: `✓/!/✗ title — detail`, hint indented); `exit_code(results) -> int` (1 if any `fail`, else 0). `--json` prints the dataclasses.
- `Controller.status()` gains `"hotkeys_grabbed"`: a comma-joined string of grabbed hotkey names from `HotkeyPort.grabbed()` (via `extra_status`).

- [ ] **Step 1: Write the failing tests** (all with a fake `DoctorEnv`):

```python
def test_all_ok_exit_0():
def test_wayland_session_fails_with_hint():
    r = by_id(run_checks(env(environ={"XDG_SESSION_TYPE": "wayland"}), CHECKS))["session"]
    assert r.status == "fail" and "Xorg" in r.hint
def test_a11y_fix_sets_gsetting():               # fix -> env.run called with [..., "set", ..., "toolkit-accessibility", "true"]
def test_daemon_down_is_fail_with_systemctl_hint():
def test_no_backend_is_warn_not_fail_and_lists_commands():
    r = by_id(...)["backend"]; assert r.status == "warn"
    for cmd in ("claude auth login", "codex login", "opencode auth login", "glance backend use"): assert cmd in (r.detail + (r.hint or ""))
def test_running_app_without_flag_is_warn_restart():
    e = env(procs=lambda: [(10, ["/usr/lib/slack/slack"]), (11, ["/usr/lib/slack/slack", "--type=renderer"])])
    r = by_id(run_checks(e, CHECKS))["running_flags"]; assert r.status == "warn" and "Restart Slack" in r.detail
def test_running_app_with_flag_is_ok():
def test_editor_globally_set_to_glance_edit_is_fail():
def test_wrapper_conflict_is_warn_with_snippet():
def test_other_electron_is_opt_in_only():        # apply_fixes(apps=[]) does not create an override for "code_code"; apps=["code_code"] does
def test_exit_code_and_render():
    assert exit_code([ok, warn]) == 0 and exit_code([ok, fail]) == 1 and render([fail]).startswith("✗")
def test_doctor_output_has_no_typed_text():      # status() dict injected with len only -> render contains no field text
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_doctor.py -v` — FAIL.
- [ ] **Step 3: Implement** (the `desktop_flags` check is stubbed to `ok` until Task 2).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: glance doctor checks`.

---

### Task 2: `.desktop` accessibility overrides with undo

**Files:**
- Create: `src/glance/doctor/desktop_entries.py`
- Modify: `src/glance/doctor/checks.py` (real `desktop_flags`), `src/glance/paths.py` (`overrides_manifest()` → `data_dir()/"desktop-overrides.json"`)
- Test: `tests/test_desktop_entries.py`

**Interfaces:**
- Produces:
  - `FLAG = "--force-renderer-accessibility"`; `DEFAULT_IDS = ("google-chrome", "com.google.Chrome", "brave-browser", "brave-browser-nightly", "com.brave.Browser", "com.brave.Browser.nightly", "slack", "discord")`; `SYSTEM_DIRS = ("/usr/share/applications", "/var/lib/snapd/desktop/applications", "/usr/local/share/applications")`.
  - `@dataclass(frozen=True) Target(id:str, source:Path, name:str, binary:str, default:bool)`.
  - `find_targets(system_dirs, user_dir:Path) -> list[Target]` — default ids present on the machine, plus *detected* Chromium/Electron apps (`default=False`): entries whose `Exec` binary resolves to a directory containing `chrome-sandbox`, `chrome_crashpad_handler`, or `resources/app.asar`.
  - `rewrite_exec(line:str) -> str` — for a line starting `Exec=`, inserts `FLAG` right after the command word (after an `env VAR=… ` prefix if present, respecting quoted paths); idempotent; other lines returned unchanged. Applied to every `Exec=` in the file, including `[Desktop Action …]` groups.
  - `install_override(t:Target, user_dir:Path, manifest:Path) -> Path` — writes `user_dir/<id>.desktop` = source with every `Exec` rewritten, plus `X-Glance-Override=true` and `X-Glance-Source-SHA256=<hash of source>` in `[Desktop Entry]`. If a non-Glance file already exists at that path: copy it to `<id>.desktop.glance-bak`, record `{"id", "backup"}` in the manifest, and build the override from the *user's* file instead of the system one. Atomic writes. Finishes with `update-desktop-database <user_dir>` (ignored if missing).
  - `patch_user_launchers(user_dir, manifest) -> list[Path]` — user-owned launchers that start a targeted browser binary (e.g. `chrome-<appid>-Default.desktop` PWAs) get the flag in place, with a `.glance-bak` backup recorded in the manifest.
  - `override_state(t, user_dir) -> str` — `missing | current | stale | foreign` (`stale`: recorded source hash ≠ current source hash; `foreign`: a user file without the flag and without our marker).
  - `undo_all(user_dir, manifest) -> list[Path]` — deletes files carrying `X-Glance-Override=true`, restores every backup byte-for-byte, deletes the manifest; safe to run repeatedly.

- [ ] **Step 1: Write the failing tests** (tmp dirs for system and user):

```python
@pytest.mark.parametrize("line, want", [
    ("Exec=/usr/bin/google-chrome-stable %U", "Exec=/usr/bin/google-chrome-stable --force-renderer-accessibility %U"),
    ("Exec=/usr/bin/google-chrome-stable --incognito", "Exec=/usr/bin/google-chrome-stable --force-renderer-accessibility --incognito"),
    ("Exec=/usr/bin/discord --url -- %u", "Exec=/usr/bin/discord --force-renderer-accessibility --url -- %u"),
    ("Exec=env BAMF_DESKTOP_FILE_HINT=x /snap/bin/code %F", "Exec=env BAMF_DESKTOP_FILE_HINT=x /snap/bin/code --force-renderer-accessibility %F"),
    ('Exec="/opt/My App/app" %U', 'Exec="/opt/My App/app" --force-renderer-accessibility %U'),
    ("Name=Slack", "Name=Slack")])
def test_rewrite_exec(line, want): assert rewrite_exec(line) == want and rewrite_exec(want) == want

def test_override_covers_all_exec_lines_and_marks_file():
    # source with main Exec + 2 action Exec lines -> 3 flagged lines; contains X-Glance-Override=true; source file untouched
def test_existing_user_entry_is_backed_up_and_restored():
    mine = "[Desktop Entry]\nName=Slack (mine)\nExec=/usr/bin/slack --my-flag %U\n"; (user / "slack.desktop").write_text(mine)
    install_override(slack, user, manifest)
    assert "--my-flag" in (user / "slack.desktop").read_text() and FLAG in (user / "slack.desktop").read_text()
    undo_all(user, manifest); assert (user / "slack.desktop").read_text() == mine and not (user / "slack.desktop.glance-bak").exists()
def test_undo_removes_only_glance_files_and_is_idempotent():
    # unrelated user file kitty.desktop untouched; undo_all twice -> no error; manifest gone
def test_override_regenerated_when_source_changes():
    install_override(chrome, user, manifest); chrome.source.write_text(chrome.source.read_text().replace("%U", "--new-flag %U"))
    assert override_state(chrome, user) == "stale"; install_override(chrome, user, manifest)
    assert "--new-flag" in (user / "google-chrome.desktop").read_text() and override_state(chrome, user) == "current"
def test_pwa_launcher_patched_in_place_and_restored():
def test_find_targets_defaults_and_detected():
    # system dir has google-chrome, slack, an Electron app dir with resources/app.asar ("obsidian"), and gedit
    assert {(t.id, t.default) for t in find_targets([sysdir], user)} == {("google-chrome", True), ("slack", True), ("obsidian", False)}
def test_doctor_fix_then_undo_round_trip():      # desktop_flags: fail -> apply_fixes -> ok -> undo_all -> fail; user dir tree equals the starting tree
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_desktop_entries.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS; `.venv/bin/pytest tests/test_doctor.py` — PASS.
- [ ] **Step 5: Commit** `feat: reversible .desktop accessibility overrides`.

---

### Task 3: Installer, uninstaller, systemd unit

**Files:**
- Create: `install.sh`, `uninstall.sh`, `packaging/glance.service`
- Modify: `src/glance/daemon/main.py` (session guard, exit 78), `README.md` (install / uninstall / doctor sections)
- Test: `tests/test_install_scripts.py`

**Interfaces:**
- `packaging/glance.service` (installed to `~/.config/systemd/user/glance.service` with `@VENV@` substituted):

  ```ini
  [Unit]
  Description=Glance writing assistant
  PartOf=graphical-session.target
  After=graphical-session.target

  [Service]
  Type=simple
  ExecStart=@VENV@/bin/glanced
  Restart=on-failure
  RestartSec=200ms
  RestartPreventExitStatus=78
  Environment=PYTHONUNBUFFERED=1

  [Install]
  WantedBy=graphical-session.target
  ```

- `glanced` start-up guard: `XDG_SESSION_TYPE != "x11"` or `DISPLAY` unset → one line on stderr (`glance: needs an X11 session; not starting`) and exit code 78.
- `install.sh [--with-apt] [--no-desktop-overrides] [--no-wrapper] [--no-start]` — steps, each printed as it runs, stopping on the first failure: (1) refuse unless `XDG_SESSION_TYPE=x11` and `/usr/bin/python3` is 3.10; (2) `uv venv --python /usr/bin/python3 --system-site-packages .venv` (skipped if present) and `uv pip install -e .`; (3) `scripts/fetch-harper.sh`; (4) symlink `glance`, `glanced`, `glance-capture`, `glance-edit` from `.venv/bin` into `~/.local/bin` (existing non-Glance files are not overwritten — abort with the path); (5) with `--with-apt`: `sudo apt-get install -y gir1.2-ayatanaappindicator3-0.1`, otherwise print that command if the GIR is missing; (6) write the unit, `systemctl --user daemon-reload`, `enable --now glance.service` (unless `--no-start`); (7) `glance doctor --fix` (overrides + wrapper, honouring the two `--no-…` flags); (8) print `glance doctor` output and, if any target app is running without the flag, "Restart: <apps>"; if no backend, the "Connect an AI" lines. Records what it created in `~/.local/share/glance/install-manifest.json`.
- `uninstall.sh [--keep-data]` — `systemctl --user disable --now glance.service` (ignore errors); `glance doctor --undo` if the CLI exists, else the same undo through `.venv/bin/python -m glance.doctor.runner --undo`; remove the unit, the four symlinks (only if they point into this repo's venv), `$XDG_RUNTIME_DIR/glance`; without `--keep-data` remove `~/.local/share/glance` (dictionary, ignored rules, overrides, harper binary) after printing what is deleted; `~/.config/glance/config.toml` is kept and its path printed. Every step is safe when its target is absent.
- Test hooks (env, honoured by both scripts): `GLANCE_HOME` (use instead of `$HOME`), `GLANCE_SKIP_SYSTEMCTL=1`, `GLANCE_SKIP_FETCH=1`, `GLANCE_SKIP_VENV=1`.

- [ ] **Step 1: Write the failing tests** (run the real scripts against a fake home; `tree(home)` = sorted `{relative path: sha256}`):

```python
def test_install_creates_expected_files(fake_home):
    run("install.sh", "--no-start"); t = tree(fake_home)
    for p in (".local/bin/glance", ".local/bin/glance-edit", ".config/systemd/user/glance.service", ".local/share/glance/install-manifest.json"): assert p in t
    assert "ExecStart=" in read(".config/systemd/user/glance.service") and "@VENV@" not in read(".config/systemd/user/glance.service")
def test_install_then_uninstall_restores_home_exactly(fake_home):
    # fake home pre-seeded with .zshrc, a user slack.desktop, an unrelated ~/.local/bin/foo
    before = tree(fake_home); run("install.sh", "--no-start"); run("uninstall.sh"); assert tree(fake_home) == before
def test_install_is_rerunnable(fake_home):                 # twice -> exit 0, one wrapper block, one manifest
def test_install_refuses_to_overwrite_foreign_binary(fake_home):   # ~/.local/bin/glance is a regular file -> exit != 0, file intact
def test_install_reports_wrapper_conflict_without_writing(fake_home):
    # .zshrc has `alias claude=…` -> exit 0, stdout mentions the manual snippet, .zshrc unchanged
def test_uninstall_is_idempotent_and_safe_on_partial_install(fake_home):
    run("uninstall.sh"); run("uninstall.sh")               # never installed: both exit 0, tree unchanged
def test_uninstall_keep_data(fake_home):                   # dictionary.txt survives with --keep-data
def test_uninstall_leaves_foreign_symlink(fake_home):      # ~/.local/bin/glance -> elsewhere is not removed
def test_unsupported_session_exits_78():
    p = subprocess.run([GLANCED], env={**os.environ, "XDG_SESSION_TYPE": "wayland"}, capture_output=True)
    assert p.returncode == 78 and b"X11" in p.stderr
def test_unit_file_has_required_directives():
    u = Path("packaging/glance.service").read_text()
    for s in ("PartOf=graphical-session.target", "Restart=on-failure", "RestartPreventExitStatus=78", "WantedBy=graphical-session.target"): assert s in u
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_install_scripts.py -v` — FAIL.
- [ ] **Step 3: Implement** scripts, unit, guard. `shellcheck install.sh uninstall.sh` clean if shellcheck is installed.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Real install on this machine.** `./install.sh --with-apt` (user enters the sudo password). Verify: `systemctl --user status glance` active; `glance doctor` shows no ✗; quit and relaunch Slack and Chrome from the GNOME launcher → `glance doctor` `running_flags` ok; log out and in → Glance is running (autostart). Record the output in `docs/acceptance/report.md` §Install.
- [ ] **Step 6: Commit** `feat: installer, uninstaller, systemd user unit`.

---

### Task 4: Resilience hardening

**Files:**
- Create: `src/glance/daemon/session.py`, `tests/acceptance/test_resilience.py`
- Modify: `src/glance/daemon/controller.py` (`handle_resume`, `handle_lock`, `handle_monitors_changed`), `src/glance/daemon/main.py`, `src/glance/capture/main.py` (`RESET` message re-registers AT-SPI listeners), `src/glance/protocol.py` (`RESET = "reset"`), `tests/desktop/gtk_app.py` (`SIGUSR2` blocks the GTK main loop for 5 s)
- Test: `tests/test_resilience_unit.py`, `tests/acceptance/test_resilience.py`

**Interfaces:**
- Produces (`session.py`): `class SessionEvents(on_sleep:Callable[[],None], on_resume:Callable[[],None], on_lock:Callable[[bool],None], on_monitors_changed:Callable[[],None])` with `start()` — logind `PrepareForSleep` (system bus), `org.gnome.ScreenSaver.ActiveChanged` (session bus), `Gdk.Screen` `monitors-changed` and `size-changed`.
- Produces (`controller.py`):
  - `handle_resume()` — bump `generation`, cancel all AI work, drop the current snapshot and hide chip/panel/ghost, clear pending apply (its late result is ignored), send `RESET` to capture, re-grab all hotkeys, refresh backend healths, drop warm backend processes.
  - `handle_lock(active:bool)` — locked: hide everything, behave as paused (no capture processing, no AI); unlocked: resume previous pause state.
  - `handle_monitors_changed()` — re-place the chip from the last snapshot with fresh `overlay.monitors()`; hide it if the anchor is now off every monitor.
- Behaviours pinned by tests: a dead `glance-capture`, `harper-ls`, `opencode serve`, or in-flight CLI child never takes `glanced` down and is restarted by its supervisor; a hung target app costs at most one 200 ms AT-SPI timeout per focus and never blocks the GTK loop; backend sign-out/rate-limit → chip state changes, local grammar unaffected; `glanced` restart ≤ 2 s.

- [ ] **Step 1: Write the failing unit tests (`tests/test_resilience_unit.py`)**

```python
def test_resume_cancels_and_resets():
    # analyze in flight + pending apply; c.handle_resume() -> supervisor.cancel_all called, capture.resets == 1, overlay.chip is None, hotkeys re-grabbed
    # late ApplyResult for the old apply_id is ignored (no notification)
def test_lock_hides_and_blocks_processing():
    c.handle_lock(True); c.handle_snapshot(snap()); clock.advance(2000); assert harper.calls == n0 and overlay.chip is None
    c.handle_lock(False); c.handle_snapshot(snap()); clock.advance(400); assert overlay.chip is not None
def test_lock_preserves_user_pause():           # paused before lock -> still paused after unlock
def test_monitor_unplug_replaces_chip():
    # chip at x=2500 on monitor 2; overlay.monitors() now returns only monitor 1 -> chip re-placed inside monitor 1 or hidden; never x >= 1920
def test_clock_jump_does_not_fire_stale_timers():   # advance(8h) after handle_resume -> no on_analyze for the pre-sleep snapshot
def test_backend_rate_limit_keeps_local_grammar():
    # backend raises rate_limit x3 -> breaker open; next snapshot still yields FIXES chip from Harper; notifier.calls == []
def test_harper_restart_recovers_issues():      # harper.alive False then True -> next snapshot shows issues again; tray back to "active"
```

- [ ] **Step 2: Write the acceptance tests (`tests/acceptance/test_resilience.py`, `@pytest.mark.desktop`)** against an isolated `glanced` (tmp XDG dirs, fake backend) and `gtk_app.py`:
  - `test_kill_capture_recovers_within_2s`, `test_kill_harper_recovers_within_2s` (after recovery, typing a misspelling yields `local_issues >= 1`).
  - `test_kill_backend_child_mid_request` — fake CLI in `hang` mode, `pkill` it → `glance status` responds, chip not stuck on spinner after 1 s.
  - `test_hung_app_does_not_freeze_daemon` — `SIGUSR2` to `gtk_app`; during the 5 s hang `glance status` answers in < 0.5 s ten times in a row; after the hang, snapshots resume.
  - `test_daemon_restart_under_2s` — only when the systemd unit is installed (skip otherwise): `systemctl --user kill -s KILL glance`; `glance status` succeeds within 2 s.
  - `test_stale_socket_after_sigkill` — run `glanced` directly, `kill -9`, start again → starts (no "address in use"), exactly one instance.
  - `test_second_instance_refuses` — exit code 1, first instance unaffected.
  - `test_suspend_resume_simulated` — emit the resume path through the test-only ctl message `test_resume` (honoured only with `GLANCE_TEST=1`) → a fix applied afterwards still verifies.
- [ ] **Step 3: Run** both files — FAIL.
- [ ] **Step 4: Implement** `session.py`, the three controller handlers, `RESET`, stale-socket handling if not already correct.
- [ ] **Step 5: Run** `.venv/bin/pytest tests/test_resilience_unit.py -v` and `.venv/bin/pytest -m desktop tests/acceptance/test_resilience.py -v` — PASS.
- [ ] **Step 6: Manual check, once:** `systemctl suspend`, wake, type in gedit → chip works; unplug/replug the second monitor with the chip on it → chip moves to the remaining monitor. Record in `docs/acceptance/report.md` §Resilience.
- [ ] **Step 7: Commit** `feat: suspend/lock/hotplug handling and resilience tests`.

---

### Task 5: Fixture pages, browser harness, capture matrix (spec §8.1)

**Files:**
- Create: `tests/acceptance/__init__.py`, `harness.py`, `fixtures/*.html`, `fixtures/src/{prosemirror,slate,lexical,quill}.js`, `fixtures/package.json`, `fixtures/dist/*.js`, `scripts/build-fixtures.sh`, `tests/acceptance/test_capture_matrix.py`
- Test: `tests/acceptance/test_harness.py`, `tests/acceptance/test_capture_matrix.py`

**Interfaces:**
- Fixture page contract — every page defines `window.glance = { getText(): string, getCaret(): number, setCaret(n), setText(s), focus() }` for its editor and, on every `input`/`selectionchange`, POSTs `{"text","caret","seq"}` to `/state`; it long-polls `GET /cmd` and executes `{"op": "setText"|"setCaret"|"focus", …}`. `getText()` is the editor's *document* text with blocks joined by `\n` (ProseMirror `doc.textBetween(0, size, "\n")`, Slate `Node.string` per block, Lexical `$getRoot().getTextContent()`, Quill `getText()` minus its trailing newline). Pages: `textarea.html`, `contenteditable.html`, `prosemirror.html` (basic schema + a mention node rendered as a non-editable inline atom), `slate.html`, `lexical.html` (plain-text plugin), `quill.html`; plus `password.html` and `login/index.html` for Task 7.
- `scripts/build-fixtures.sh` — `cd tests/acceptance/fixtures && npm ci && npx esbuild src/*.js --bundle --minify --outdir=dist` using the nvm Node; versions pinned in `package.json`/`package-lock.json`; `dist/` is committed so running tests needs no Node.
- Produces (`harness.py`):
  - `class FixtureServer(root:Path)`: `start() -> str` (base URL on `127.0.0.1`, random port), `state() -> dict` (latest POST), `wait_state(pred, timeout_s) -> dict`, `cmd(op:str, **kw) -> None`, `stop()`.
  - `class Browser(kind:str, profile_root:Path)`: kinds `chrome` (`/usr/bin/google-chrome-stable`), `brave` (`/usr/bin/brave-browser-nightly`), `firefox`; `open(url, incognito=False)` launches with a throwaway profile (`--user-data-dir=… --no-first-run --no-default-browser-check --force-renderer-accessibility`; Firefox: `-profile … -no-remote -new-instance`, profile under `~/.cache/glance-test/` because the snap cannot see `/tmp`, with the prefs findings S3.2 require), waits for the page's first `/state`; `close()` terminates the process group and deletes the profile.
  - `class Typist`: XTEST `type(text)`, `key(name, n=1)`, `chord(*keys)`, `click(x, y)`; 8 ms between keys.
  - `class GlanceUnderTest(tmp)`: starts an isolated `glanced` (tmp XDG dirs, `GLANCE_TEST=1`, `GLANCE_FAKE_BACKEND=1`) with a test tap — ctl message `test_state` returning the last `FieldSnapshot` **with text** and the current `ChipView` (only under `GLANCE_TEST=1`); `last_snapshot(timeout_s)`, `chip()`, `status()`, `stop()`.
  - `edit_script(seed:int, n:int=200) -> list[Op]` — deterministic mix: 45 % type a word (from a fixed 200-word list incl. punctuation and two emoji), 15 % Backspace ×1–5, 10 % Left/Right ×1–8, 10 % Home/End, 8 % `setCaret` to a random offset (simulates a mouse click), 7 % paste a word (clipboard + Ctrl+V), 5 % select-left ×1–6 then type.
  - `norm(s:str) -> str` — NFC, U+00A0→space, strip one trailing `\n` (the same normalisation as `WriteBackEngine`).
  - `run_capture_matrix(target, n=200, seed=1) -> MatrixResult(total, exact, mismatches:list[dict])` — after each op waits until page `seq` and Glance `seq` are both quiet for 120 ms, then compares `norm(snapshot.text) == norm(state.text)` and `snapshot.caret == state.caret`. Mismatch records hold op index, op kind, lengths and caret values only.
- Gate: `exact / total >= 0.99` for every (browser, fixture) pair and for the GTK app (ground truth via `gtk_app.py`'s `SIGUSR1` dump). Claude Code via Ctrl+G is covered by Plan 3 Task 7's round-trip test plus the smoke checklist.

- [ ] **Step 1: Write `tests/acceptance/test_harness.py`** (no browser): `test_edit_script_is_deterministic` (same seed → same ops; 200 ops; all seven kinds present), `test_norm`, `test_fixture_server_state_and_cmd` (POST `/state` with `httpx`, read `state()`; `cmd` delivered to a polling client), `test_dist_bundles_exist_and_match_lockfile` (each `dist/*.js` present; `package-lock.json` present).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/acceptance/test_harness.py -v` — FAIL.
- [ ] **Step 3: Implement** the harness, pages, bundles (`scripts/build-fixtures.sh`). Run — PASS.
- [ ] **Step 4: Write `test_capture_matrix.py`** (`@pytest.mark.apps`): parametrized over `chrome|brave|firefox` × the six pages (skip a browser that findings marked NO-GO, with the reason in the skip message) and `gtk`: `r = run_capture_matrix(target); assert r.exact / r.total >= 0.99, r.mismatches[:5]`. Also `test_mention_atom_reported_as_object` (ProseMirror page with a mention → snapshot text contains U+FFFC at the atom) and `test_caret_rect_tracks_caret` (caret rect x strictly increases while typing one line; within the field rect).
- [ ] **Step 5: Run** `.venv/bin/pytest -m apps tests/acceptance/test_capture_matrix.py -v` in the GNOME session (hands off keyboard and mouse; ~20 min). For each failing pair: fix the cause in `atspi_port.py` / `tracker.py` (typical: stale text after paste → re-read on `text-changed:insert`; caret off by one at line ends; U+FFFC accounting), add a unit test to `tests/test_tracker.py` reproducing it with `FakeField`, re-run. A pair that cannot reach 99 % is recorded in the report with its number and gets `[apps.<wm_class>]`-level handling only if the editor is one the user actually uses; otherwise it stays a documented failure — do not lower the gate.
- [ ] **Step 6: Commit** `test: fixture pages, browser harness, capture matrix`; append the matrix table to `docs/acceptance/report.md` §Capture.

---

### Task 6: Write-back matrix (spec §8.2)

**Files:**
- Create: `tests/acceptance/test_writeback_matrix.py`
- Modify: `tests/acceptance/harness.py` (`run_writeback_matrix`)
- Test: `tests/acceptance/test_writeback_matrix.py`

**Interfaces:**
- Consumes: Task 5 harness; ctl test message `test_apply {start,end,replacement,method}` (under `GLANCE_TEST=1`) that calls `controller.request_apply` and returns the `ApplyResult`.
- Produces: `run_writeback_matrix(target, n=500, seed=2) -> WbResult(total, verified, reverted, refused, stale, failed, silent_corruptions:list[dict], undo_checked, undo_ok, clipboard_violations)`. Per iteration: page reset to a 3-sentence seed text every 25 applies (`setText`); clipboard set to the sentinel `CLIP-SENTINEL-<i>`; pick a random range (0–60 chars; 10 % empty = pure insert; 10 % whole text) and a replacement (40 % ≤ 20 chars typed, 40 % 81–400 chars pasted, 10 % empty = delete, 10 % containing non-ASCII `é — 😀`); `before = page text`; apply; wait quiet; `after = page text`. Classification:
  - `verified` and `norm(after) == norm(expected)` → ok. `verified` and different → **silent corruption**.
  - `reverted | refused | stale | failed` and `norm(after) == norm(before)` → ok (counted). Any of them with `after != before` → **silent corruption** (for `failed`, corruption is *reported* to the user, so it is counted separately as `failed_dirty` and must also be 0 on fixtures).
  - After every 10th verified apply: one XTEST Ctrl+Z → page text must equal `before` (`undo_ok`).
  - After every apply: clipboard text must equal the sentinel, else `clipboard_violations += 1`.
- Gates per target: `silent_corruptions == []`, `failed_dirty == 0`, `undo_ok == undo_checked`, `clipboard_violations == 0`, and `verified / total >= 0.95` (a matrix that passes by refusing everything is not a pass).

- [ ] **Step 1: Write the test** (`@pytest.mark.apps`), parametrized like Task 5; plus `test_apply_while_typing_never_corrupts` — a background thread types one char every 30 ms into the page while 100 applies are requested; gate: zero silent corruption (most results will be `stale`), and the final page text contains every typed character in order.
- [ ] **Step 2: Run** `.venv/bin/pytest -m apps tests/acceptance/test_writeback_matrix.py -v` (~40 min, hands off). Expected first run: failures.
- [ ] **Step 3: Fix causes** in `writeback/engine.py`, `keys_xlib.py`, `clipboard_gtk.py` or per-app defaults in `config.py` (`select`, `undo_steps`, `type_delay_ms`, `type_max_chars`), each with a unit test in `tests/test_writeback.py` that reproduces the editor behaviour through a `FakeField` knob. Where an editor makes one replace two undo steps, set that editor's `undo_steps` default (keyed by wm_class; for web editors the default must hold for all pages in that browser, so prefer making the replace atomic — select then paste — over raising `undo_steps`).
- [ ] **Step 4: Re-run** until every gate holds for every non-skipped target.
- [ ] **Step 5: Commit** `test: write-back matrix with zero silent corruption`; append the table to `docs/acceptance/report.md` §Write-back.

---

### Task 7: Privacy proofs (spec §8.5)

**Files:**
- Create: `scripts/gen-secret-corpus.py`, `tests/acceptance/corpus/secrets.jsonl`, `corpus/benign.jsonl`, `tests/acceptance/test_redaction_corpus.py`, `test_privacy_e2e.py`, `test_disk_audit.py`
- Modify: `tests/desktop/gtk_app.py` (`--wm-class NAME` sets the program class), `tests/fake_cli/*` (append every stdin to `$FAKE_STDIN_LOG`)
- Test: the three files above

**Interfaces:**
- `scripts/gen-secret-corpus.py --seed 7` writes deterministic, synthetic data: `secrets.jsonl` — 600 lines `{"text": sentence, "secret": substring, "kind": k}`, 100 per kind for `api_key` (spread over all prefixes in `find_secrets`), `jwt`, `pem`, `card` (Luhn-valid, spaced/dashed/plain), `assignment`, `entropy`, each embedded in one of 30 sentence templates (start, middle, end, inside quotes/backticks, after a colon). `benign.jsonl` — 400 lines `{"text": …}`: prose, URLs, git SHAs in a "commit abc1234" form (7–12 hex), phone numbers, order numbers, long ordinary words, file paths, UUIDs.
- Gates: recall = secrets whose span is fully covered by a `find_secrets` span ≥ 99 % overall and ≥ 97 % per kind; benign false-positive rate ≤ 3 % (full-length 40-hex SHAs and UUIDs are *allowed* to be redacted and are excluded from the benign set); `shield(text).text` never contains the secret.

- [ ] **Step 1: Write `test_redaction_corpus.py`** (default run, no marker): `test_recall_overall_at_least_99`, `test_recall_per_kind_at_least_97`, `test_shielded_text_never_contains_secret`, `test_benign_false_positive_rate_at_most_3_percent`, `test_corpus_is_reproducible` (regenerating with seed 7 gives identical files).
- [ ] **Step 2: Run** `.venv/bin/pytest tests/acceptance/test_redaction_corpus.py -v` — FAIL (no corpus). Generate it; run again; tighten `privacy/redact.py` patterns until the gates hold, adding each missed shape to `tests/test_redact.py` as a named case.
- [ ] **Step 3: Write `test_privacy_e2e.py`** (`@pytest.mark.apps`) — isolated `glanced` with the **fake CLI backend** selected (so the full shield → argv/stdin path runs) and `FAKE_STDIN_LOG` recording everything any backend receives; the marker sentence is `ZQXJ-marker Teh quick brown fox jump over the lazy dog today.` Each test types the marker, waits 3 s (past the analyze pause), then asserts `status()["field"]["gated"] == <reason>`, the tapped last snapshot has `text == ""`, and `ZQXJ` is absent from `FAKE_STDIN_LOG`:
  - `test_password_field_gtk` (gtk_app password entry), `test_password_field_web` (`password.html` in Chrome),
  - `test_denylisted_app` (parametrized `--wm-class` ∈ `Remmina, AnyDesk, Seahorse, KeePassXC`),
  - `test_incognito_chrome`, `test_private_firefox` (`Browser.open(…, incognito=True)`),
  - `test_login_url` (`/login/` page), `test_terminal_class` (`--wm-class gnome-terminal-server`),
  - `test_paused` (`glance pause` → nothing processed),
  - `test_control_positive` — the same marker in `textarea.html` **does** reach `FAKE_STDIN_LOG` (proves the tap works), and a typed `sk-ant-api03-…` fake key in that field appears there only as `[[GLANCE_S1]]`.
  - `test_typed_text_never_in_argv` — while a `hang`-mode fake CLI is running, no `/proc/<pid>/cmdline` of any `glanced` descendant contains `ZQXJ`.
- [ ] **Step 4: Write `test_disk_audit.py`** (`@pytest.mark.apps`) — one 3-minute scripted session (typing 40 marker sentences `ZQXJ-<n> …` across gtk_app and `textarea.html`, applying fixes, a rewrite, an autocomplete accept, a select-anything, with daemon stderr captured to a file, then stopping Glance). Assert `grep -r ZQXJ` finds nothing in: the tmp XDG config/data/runtime/cache dirs, the stderr capture, `empty_dir()`, and `/tmp/glance*`; `os.listdir(empty_dir()) == []`. `test_only_expected_files_exist` — the data dir contains at most `dictionary.txt, ignored.json, overrides.json, usage.json, desktop-overrides.json, install-manifest.json, bin/`. `test_dictionary_add_is_the_only_text_write` — after "Add to dictionary" on `ZQXJword`, the only file containing `ZQXJword` is `dictionary.txt`.
- [ ] **Step 5: Run** `.venv/bin/pytest -m apps tests/acceptance/test_privacy_e2e.py tests/acceptance/test_disk_audit.py -v` — fix any leak at its source; PASS.
- [ ] **Step 6: Backend residue (manual, one paid call per connected backend, with the user's go-ahead):** note the newest entry under `~/.claude/projects`, `~/.codex/sessions`, `gemini --list-sessions`, `opencode session list`; run one `glance backend test <name>`; confirm no new entry. Write the result per backend into `docs/acceptance/report.md` §Privacy and make `STORAGE_NOTES` (Plan 2) match what was observed.
- [ ] **Step 7: Commit** `test: privacy proofs — redaction corpus, gate e2e, disk audit`.

---

### Task 8: Latency and resource gates (spec §8.3, §8.4)

**Files:**
- Create: `tests/acceptance/test_latency.py`, `test_resources.py`
- Modify: `src/glance/daemon/controller.py` and `ai.py` (timing events via `log_event`: `chip_shown ms_since_settle`, `chip_moved ms`, `analyze_done ms backend`, `rewrite_first_delta ms backend`, `apply_sent ms_since_request`, `ghost_shown ms_since_pause`), `tests/acceptance/harness.py` (`timings(event) -> list[float]` parsed from the daemon's stderr capture; `proc_cpu(pids, seconds) -> float` from `/proc/<pid>/stat`; `rss_mb(pids)` from `/proc/<pid>/status`)
- Test: the two files above

**Interfaces:**
- Timing events carry numbers and the backend name only (they pass `GLANCE_STRICT_LOG=1`).
- Gates (fixtures / fake backend unless marked `paid`):

| Test | Method | Gate |
|---|---|---|
| `test_local_fix_latency` | 50 misspelling edits in gtk_app; `chip_shown.ms_since_settle` | p95 ≤ 150 ms |
| `test_chip_reposition` | 100 caret moves (Left/Right); `chip_moved.ms` | p95 ≤ 50 ms |
| `test_pregenerated_apply` | fake `api` backend, speculation on; hotkey → `apply_sent` | p95 ≤ 200 ms |
| `test_tab_never_stolen` | autocomplete live enabled, backend returns no completion; 1000 Tab presses in gtk_app | buffer has exactly 1000 `\t` |
| `test_no_added_keystroke_latency` | 2000 XTEST keys into gtk_app at 20 ms spacing; per-key delay until the buffer changes, measured by the app itself (`--timing` mode prints deltas); run with Glance stopped and running | difference of medians < 1 ms, and p99 difference < 5 ms |
| `test_no_ui_while_typing` | sample chip visibility every 20 ms during a 20 s typing burst (≥ 5 keys/s) | never visible |
| `test_analyze_p50_cli` (`paid`) | 10 analyze calls on the chosen CLI backend; `analyze_done.ms` | p50 ≤ 3500 ms |
| `test_rewrite_first_token_cli` (`paid`) | 5 rewrites; `rewrite_first_delta.ms` | p50 ≤ 3500 ms |
| `test_api_latencies` (`paid`, skipped without an API key) | 10 analyze, 5 rewrite, 10 live completions | p50 ≤ 1200 / ≤ 1000 / ≤ 1200 ms |
| `test_idle_cpu` | Glance running, focus in gtk_app, no input for 60 s; CPU of glanced + glance-capture + harper-ls | < 0.5 % of one core |
| `test_typing_cpu` | 60 s of typing at 6 keys/s | < 3 % of one core |
| `test_rss` | after the typing run | sum RSS < 250 MB (backend workers excluded) |
| `test_chrome_a11y_overhead` (`apps`, report-only) | Chrome CPU on `textarea.html`, idle 60 s and typing 30 s, with and without the flag | numbers written to the report; no gate |

- [ ] **Step 1: Write both test files** (`@pytest.mark.desktop` unless noted). Each test prints its measured p50/p95 so the report can quote them.
- [ ] **Step 2: Run** `.venv/bin/pytest -m desktop tests/acceptance/test_latency.py tests/acceptance/test_resources.py -v` — expect some failures.
- [ ] **Step 3: Fix causes** — typical: idle CPU from polling (make `ChildSupervisor.poll` and `ConfigWatcher` the only timers, ≥ 500 ms; no timer while paused/locked); AT-SPI event storms from Chrome (drop `text-caret-moved` events for non-focused objects in `AtspiPort` before any call); Harper called per keystroke (it must run on the 80 ms quiet timer only). Each fix gets a unit test where one is possible (e.g. `test_no_reads_for_unfocused_object_events` in `tests/test_tracker.py`).
- [ ] **Step 4: Re-run** — PASS. With the user's go-ahead run the `paid` tests once (≈ 15 CLI calls; ≈ 25 API calls if a key is set) and the `apps` overhead test.
- [ ] **Step 5: Commit** `test: latency and resource gates`; write the numbers into `docs/acceptance/report.md` §Latency and §Resources (including the Chrome accessibility overhead, required by spec §8.4).

---

### Task 9: Live-app smoke checklist and acceptance report

**Files:**
- Create: `docs/acceptance/smoke-checklist.md`, `docs/acceptance/report.md` (final form)
- Modify: `README.md`

**Interfaces:**
- `smoke-checklist.md` — one section per live target: **Slack**, **Discord**, **Gmail compose (Chrome)**, **claude.ai composer (Chrome)**, **ChatGPT composer (Brave)**, **Claude Code via Ctrl+G (Ghostty)**. Header rule in bold: *unsent drafts only — never press Enter/Send; clear the draft at the end of each section.* Slack uses the user's own DM-to-self; Discord a private test channel; Gmail a new draft with no recipient. Each section is the same numbered script with a pass/fail box and a count field:
  1. Focus the composer, type `Teh quick brown fox jump over the lazy dog, i think its fine.` — chip appears only after you stop typing. ☐
  2. 20 capture probes: after each of 20 listed edits (the list is fixed in the file: append word, Backspace ×3, click mid-text and type, paste a word, Home/End, select-and-type, Shift+Enter newline, add a mention, add an emoji, …) run `glance status` and compare `len` with the composer. Count exact: __/20 (target ≥ 99 % → 20/20).
  3. 10 applies: accept 5 single fixes, Fix all (Super+Alt+F), 2 tone rewrites by click, 1 by Super+Alt+1, 1 "Rewrite as…". After each: text is right or was restored with a notice — never silently wrong. Count clean: __/10. One Ctrl+Z restores after 3 of them. Clipboard still holds `CLIP-SENTINEL`. ☐
  4. A draft containing a mention and an emoji: grammar fixes do not touch them; a tone rewrite either keeps them exactly or offers "Copy rewrite". ☐
  5. Tone badge matches the context (channel vs DM vs email vs AI prompt; chips listed per profile). ☐
  6. Send-guard (Slack/Discord only): type `I've attached the file` → amber chip, Enter is not intercepted (do not press it; verify by `glance status` showing no grab on Return). ☐
  7. Autocomplete: Super+Alt+Enter shows ghost text; Tab accepts; Tab is normal afterwards. ☐
  8. Privacy spot checks: the app's own password/search-with-secrets field shows Private or nothing; in an incognito window the chip is grey. ☐
  9. Banglish: type `ami kal office e ashbo na. I will sned the file.` — only `sned` is flagged. ☐
  - Claude Code section replaces 2–3 with: Ctrl+G opens the Glance window with the prompt; Apply returns edited text; Esc returns it unchanged; "Open in my editor" opens the real editor; `echo $EDITOR` unchanged; `git commit` uses the normal editor.
- `report.md` — final structure: Summary table (one row per spec §8 criterion 1–8: gate, measured, PASS/FAIL, evidence link to the section), then §Install, §Capture, §Write-back, §Latency, §Resources, §Privacy, §Resilience, §No-backend state, §Live-app smoke results, §Known limits (every skipped/NO-GO target with its reason, Firefox status, residual backend storage). No section may say PASS without a number or a command output next to it.

- [ ] **Step 1: Write `smoke-checklist.md`** exactly as specified, with the 20 edits and 10 applies enumerated.
- [ ] **Step 2: No-backend acceptance (spec §8.7), automated:** add `tests/acceptance/test_no_backend.py` (`@pytest.mark.desktop`) — isolated `glanced` with no `backend.choice` and fake CLIs reporting signed-out: `glance backend list` first line starts `No AI connected`; `status()` shows `ai == "none"`; typing a misspelling in gtk_app still yields `local_issues >= 1`; the tapped chip view is `FIXES` with badge `none`; `glance doctor --json` has `backend` = `warn`. Run it — PASS.
- [ ] **Step 3: Full automated pass.** Run in order and paste the summary lines into the report: `.venv/bin/pytest` · `.venv/bin/pytest -m desktop` · `.venv/bin/pytest -m apps` · (with go-ahead) `.venv/bin/pytest -m paid`. Every gate in Global Constraints must be green or listed under Known limits with the user's explicit acceptance.
- [ ] **Step 4: Hand the checklist to the user** and run it together in their real profiles (≈ 30 min). Record counts per app in §Live-app smoke results. Misses on live apps are targets, not gates: file each as a line under Known limits with the app, the step and what happened.
- [ ] **Step 5: Uninstall/reinstall proof (spec §8.8).** `./uninstall.sh --keep-data`; verify: `ls ~/.local/share/applications | grep -c glance-bak` is 0, no file there contains `X-Glance-Override`, `grep -c "glance claude wrapper" ~/.zshrc` is 0, `zsh -ic 'print -r -- "$EDITOR"'` is what it was before install (recorded in §Install), `systemctl --user is-enabled glance` reports not-found. Then `./install.sh` again and `glance doctor` is green. Record both outputs.
- [ ] **Step 6: Finish `report.md` and `README.md`** (what Glance does, install, the hotkey table, privacy summary, `glance doctor`, uninstall, Known limits link). Commit `docs: acceptance report, smoke checklist, readme`.
