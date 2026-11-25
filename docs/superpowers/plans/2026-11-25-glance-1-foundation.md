# Glance Plan 1 — Foundation (offline grammar, in place) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running `glanced` + `glance-capture` pair that follows the focused text field over AT-SPI, shows a chip with Harper's local grammar fixes next to the caret, and applies a fix in place with verify-or-revert — fully offline, no AI.

**Architecture:** Two processes talk newline-delimited JSON over a 0600 unix socket. `glance-capture` owns everything that touches other apps (AT-SPI reads, XTEST/clipboard write-back) and applies the hard privacy gate before any text crosses the socket. `glanced` owns scheduling, Harper, the GTK overlay, hotkeys and tray. All logic lives in plain classes that take *ports* (protocols for AT-SPI, keys, clipboard, overlay, clock); the real GTK/Xlib/Atspi adapters are thin and are the only code that needs a live desktop.

**Tech Stack:** Python 3.10 (system `/usr/bin/python3`) in a `uv` venv with `--system-site-packages`; PyGObject (Gtk 3, Gdk, Atspi 2.0, Wnck, Notify); python-xlib; `tomli`; `harper-ls`; pytest; ruff.

**Spec:** `docs/superpowers/specs/2026-11-25-glance-design.md`. Spike facts: `docs/superpowers/spike/findings.md` — read its `## Verdict` first; where it records a GO-WITH-CHANGE for this plan, the findings value overrides the default written here.

**Plan sequence:** 0 spike → **1 foundation (this)** → 2 backends/tone → 3 features → 4 install/hardening.

## Global Constraints

- Interpreter is `/usr/bin/python3` 3.10.12; venv created with `uv venv --python /usr/bin/python3 --system-site-packages .venv`. No `tomllib` on 3.10 — use `tomli`. Never import `gi` at module top level outside `*_gtk.py`, `*_xlib.py`, `atspi_port.py`, `*/main.py`.
- X11 only. GTK 3 only (GTK 4 removed absolute positioning). No xdotool/xclip/evdev/uinput.
- IPC: newline-delimited JSON over `$XDG_RUNTIME_DIR/glance/*.sock`, directory mode 0700, sockets mode 0600.
- Every AT-SPI call has a 200 ms timeout.
- Never write typed text to disk: no text in logs (lengths/hashes only), no cache on disk, no temp files. The personal dictionary and ignored rules in `~/.local/share/glance/` are the only exempt stores.
- Never use AT-SPI `EditableText` to change text. Write-back is select → XTEST type (≤ 80 chars) or clipboard swap + Ctrl+V → re-read after 150 ms → on mismatch Ctrl+Z and offer "Copy instead". One apply = one native undo step.
- Never touch a protected entity (mention, emoji, link, code span, U+FFFC embedded object) with an edit.
- Overlay never takes focus; chip is 22 px, appears only after ≥ 15 chars and a pause, never while keys are going down. No change is ever applied without a click or hotkey.
- Nothing leaves capture for password fields, denylisted apps (password managers, Seahorse, AnyDesk, Remmina, VM/RDP), incognito/private windows, denylisted URLs, or terminals (allowlist-only, default empty).
- Hotkeys (XGrabKey): Super+Alt+Space panel · Super+Alt+F fix all safe grammar · Super+Alt+Z revert · Super+Alt+P pause · Esc dismiss/snooze (grabbed only while the panel is open).
- Tests that need a live X session and AT-SPI bus carry `@pytest.mark.desktop`; the default run (`.venv/bin/pytest`) excludes them (`addopts = -m "not desktop and not apps and not paid"`). Run them with `.venv/bin/pytest -m desktop` from a terminal inside the GNOME session.
- Config at `~/.config/glance/config.toml`, hot-reloaded.

## Review Focus

1. The user keeps typing between clicking a fix and the apply landing → the apply must do nothing and report `stale`; no keystroke may be synthesized (Task 7, `test_apply_is_stale_when_text_changed`).
2. Text containing emoji / astral characters before the error → Harper's UTF-16 columns must map to the right code-point span (Task 6, `test_utf16_columns_map_to_codepoints`).
3. The clipboard holds an image when a long fix is pasted → the image must still be in the clipboard afterwards, or the paste path must not be used (Task 7, `test_paste_refused_when_clipboard_not_restorable`).
4. `config.toml` is saved half-written or invalid while Glance runs → the last good config stays active, nothing crashes, the user is told once (Task 3, `test_reload_keeps_last_good_on_invalid_toml`).
5. Chromium reports a garbage caret rect (`0,0,0,0`, `-1,-1,…`) or the caret is on a monitor with a negative origin → the chip uses the next fallback and stays on the caret's monitor (Task 8, `test_garbage_caret_rect_falls_back`, `test_negative_origin_monitor`).

## File Structure

```
pyproject.toml  .gitignore  README.md  scripts/fetch-harper.sh
src/glance/
  paths.py            XDG locations
  protocol.py         wire dataclasses + encode/decode
  ipc.py              JsonLineServer / JsonLineClient
  config.py           Config dataclasses, load_config, ConfigWatcher
  log.py              log_event (no free text)
  clock.py            Clock protocol, GLibClock
  privacy/gate.py     PrivacyGate           privacy/entities.py  find_protected
  capture/ports.py    A11yField/A11yPort    capture/tracker.py   FocusTracker
  capture/atspi_port.py (desktop)           capture/main.py      glance-capture
  writeback/ports.py  KeyPort/ClipboardPort writeback/engine.py  WriteBackEngine
  writeback/keys_xlib.py, clipboard_gtk.py (desktop)
  grammar/issues.py   Issue, merge_edits    grammar/harper.py    HarperClient
  grammar/store.py    PersonalDictionary, IgnoredRules   grammar/filters.py IssueFilter
  ui/chip_model.py    ui/placement.py    ui/panel_model.py    ui/hotkeys.py
  ui/overlay_gtk.py   ui/hotkeys_xlib.py ui/tray_gtk.py (desktop)
  daemon/scheduler.py daemon/controller.py daemon/supervisor.py daemon/main.py
  cli.py
tests/  fakes.py  conftest.py  test_*.py  desktop/gtk_app.py  desktop/test_*.py
```

---

### Task 1: Project skeleton, paths, wire protocol

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `README.md`, `src/glance/__init__.py`, `src/glance/paths.py`, `src/glance/protocol.py`, `tests/conftest.py`
- Test: `tests/test_protocol.py`, `tests/test_paths.py`
- Delete: `spike/` (keep `docs/superpowers/spike/`)

**Interfaces:**
- Produces (`paths.py`): `runtime_dir() -> Path` (`$XDG_RUNTIME_DIR/glance`, created 0700), `capture_sock() -> Path` (`…/capture.sock`), `ctl_sock() -> Path` (`…/ctl.sock`), `config_path() -> Path`, `data_dir() -> Path` (`~/.local/share/glance`), `dictionary_path()` (`…/dictionary.txt`), `ignored_path()` (`…/ignored.json`), `harper_bin()` (`…/bin/harper-ls`).
- Produces (`protocol.py`), all `@dataclass(frozen=True)` with `to_dict()` / `from_dict(d)`:
  - `Rect(x:int, y:int, w:int, h:int)`
  - `FieldSnapshot(app:str, wm_class:str, title:str, url:str|None, role:str, text:str, caret:int, caret_rect:Rect|None, prev_char_rect:Rect|None, field_rect:Rect|None, window_rect:Rect|None, field_id:str, capture_confidence:float, seq:int, pid:int, gated:str|None)` — when `gated` is set, `text == ""` and `caret == 0`.
  - `Edit(start:int, end:int, replacement:str)` — code-point offsets into the snapshot text.
  - `ApplyRequest(apply_id:str, field_id:str, expected_sha256:str, edit:Edit, method:str)` — method ∈ `auto|type|paste`.
  - `ApplyResult(apply_id:str, status:str, detail:str)` — status ∈ `verified|reverted|stale|failed|refused`.
  - `text_sha256(text:str) -> str`; `encode(msg_type:str, payload:dict) -> bytes` (one line, `\n`-terminated, `{"t":…, "p":…}`); `decode(line:bytes) -> tuple[str, dict]`; raises `ProtocolError` on bad JSON, missing keys, or a line over `MAX_LINE = 4 * 1024 * 1024`.
  - Message type constants: `SNAPSHOT="snapshot"`, `FOCUS_LOST="focus_lost"`, `APPLY="apply"`, `REVERT="revert"`, `APPLY_RESULT="apply_result"`, `HELLO="hello"`, `PING="ping"`.

- [ ] **Step 1: Skeleton.** `git init -b main` if `.git` is absent. `pyproject.toml`: build backend `hatchling`, package `src/glance`, `requires-python = ">=3.10,<3.11"`, deps `tomli>=2`, dev extra `pytest>=8`, `ruff>=0.6`; scripts `glance = glance.cli:main`, `glanced = glance.daemon.main:main`, `glance-capture = glance.capture.main:main`; `[tool.pytest.ini_options] addopts = '-m "not desktop and not apps and not paid"'`, markers `desktop`, `apps`, `paid`; `[tool.ruff] line-length = 100, target-version = "py310"`. Run `uv venv --python /usr/bin/python3 --system-site-packages .venv && uv pip install -e ".[dev]"`. Verify: `.venv/bin/python -c "import gi, tomli, glance"` exits 0. `git rm -r spike` if tracked.

- [ ] **Step 2: Write the failing tests**

```python
def test_snapshot_round_trips():
    s = FieldSnapshot(app="gedit", wm_class="gedit", title="t", url=None, role="text", text="héllo 😀",
                      caret=3, caret_rect=Rect(1, 2, 1, 18), prev_char_rect=None, field_rect=None,
                      window_rect=None, field_id="1:a", capture_confidence=1.0, seq=7, pid=1, gated=None)
    t, p = decode(encode(SNAPSHOT, s.to_dict()))
    assert t == SNAPSHOT and FieldSnapshot.from_dict(p) == s

def test_encode_is_single_line_even_with_newlines_in_text():
    assert encode(SNAPSHOT, {"text": "a\nb"}).count(b"\n") == 1

def test_decode_rejects_garbage_and_oversize():
    for bad in (b"not json\n", b'{"p":{}}\n', b"x" * (MAX_LINE + 1)):
        with pytest.raises(ProtocolError): decode(bad)

def test_runtime_dir_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert stat.S_IMODE(runtime_dir().stat().st_mode) == 0o700
    assert capture_sock() == tmp_path / "glance" / "capture.sock"
```

- [ ] **Step 3: Run** `.venv/bin/pytest tests/test_protocol.py tests/test_paths.py -v` — Expected: FAIL (ImportError).
- [ ] **Step 4: Implement** `paths.py` and `protocol.py` per the Interfaces block.
- [ ] **Step 5: Run** the same command — Expected: PASS. `.venv/bin/ruff check .` — clean.
- [ ] **Step 6: Commit** `git add -A && git commit -m "feat: project skeleton, paths, wire protocol"`.

---

### Task 2: JSON-lines IPC

**Files:**
- Create: `src/glance/ipc.py`
- Test: `tests/test_ipc.py`

**Interfaces:**
- Consumes: `protocol.encode/decode`.
- Produces:
  - `class Conn: send(msg_type:str, payload:dict) -> None; close() -> None`
  - `class JsonLineServer(path:Path, on_message:Callable[[Conn,str,dict],None], on_disconnect:Callable[[Conn],None]|None=None)` with `start()` (unlinks a stale socket, binds, `chmod 0600`, accept + reader threads), `stop()`.
  - `class JsonLineClient(path:Path, on_message:Callable[[str,dict],None]|None=None, on_disconnect:Callable[[],None]|None=None)` with `connect(timeout_s:float=5.0) -> None` (retries every 0.2 s, raises `ConnectionError`), `send(msg_type, payload)`, `request(msg_type, payload, timeout_s:float=3.0) -> dict` (adds `"id"`, waits for the reply carrying the same `"id"`), `close()`.
  - Callbacks run on reader threads; callers marshal to their own loop. A line that fails `decode` closes that connection and nothing else.

- [ ] **Step 1: Write the failing tests** — `test_round_trip_both_directions` (server echoes; client receives the same payload), `test_socket_mode_is_0600`, `test_request_matches_reply_by_id` (two concurrent `request` calls get their own replies), `test_bad_line_closes_only_that_connection` (second client still works), `test_server_start_replaces_stale_socket_file`, `test_client_connect_times_out` (`pytest.raises(ConnectionError)` within 1 s using `timeout_s=0.5`), `test_on_disconnect_called_when_peer_dies`.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_ipc.py -v` — FAIL.
- [ ] **Step 3: Implement** with `socket.AF_UNIX` + `socket.makefile("rb")` reader threads (daemon threads); a `threading.Lock` around each `sendall`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: json-lines ipc`.

---

### Task 3: Config, hot reload, text-free logging, clock

**Files:**
- Create: `src/glance/config.py`, `src/glance/log.py`, `src/glance/clock.py`, `tests/fakes.py`
- Test: `tests/test_config.py`, `tests/test_log.py`

**Interfaces:**
- Produces (`clock.py`): `class Clock(Protocol): now() -> float; call_later(ms:int, fn:Callable[[],None]) -> Handle; post(fn) -> None` and `Handle.cancel()`. `GLibClock` implements it with `GLib.timeout_add` / `GLib.idle_add` (imports `gi` lazily). `tests/fakes.py`: `FakeClock` with `advance(ms)` that fires due callbacks in order and runs `post` immediately.
- Produces (`config.py`), frozen dataclasses with these defaults:
  - `GeneralCfg(chip_idle_ms=400, analyze_idle_ms=1200, min_chars_chip=15, min_chars_analyze=25, max_field_chars=20000, hotkeys={"panel":"Super+Alt+space","fix_all":"Super+Alt+f","revert":"Super+Alt+z","pause":"Super+Alt+p"})`
  - `PrivacyCfg(app_denylist=(…), url_denylist=(…), incognito_title_patterns=(…), browser_wm_classes=("google-chrome","brave-browser","brave-browser-nightly","firefox","chromium"), terminal_wm_classes=("com.mitchellh.ghostty","ghostty","kitty","gnome-terminal-server","xterm","alacritty","org.wezfurlong.wezterm","konsole","tilix"), terminal_allowlist=())` — `app_denylist` default: `anydesk, remmina, seahorse, keepassxc, keepass2, bitwarden, 1password, enpass, lastpass, virt-manager, virtualbox, vmware, gnome-boxes, vncviewer, xfreerdp, rustdesk, teamviewer` (casefolded substring match on wm_class and app name); `url_denylist` default regexes: `(^|\.)accounts\.google\.com`, `login\.microsoftonline\.com`, `(^|\.)okta\.com`, `(^|\.)auth0\.com`, `(^|\.)paypal\.com`, `bank`, `/(login|signin|sign-in|sso|oauth|checkout|password)(/|\?|$)`; `incognito_title_patterns` default `("(Incognito)", "Incognito", "Private Browsing", "Private window", "InPrivate")` unless findings S2.7 records other strings.
  - `AppCfg(capture=True, writeback="auto", select="auto", undo_steps=1, type_max_chars=80, type_delay_ms=2)`
  - `UiCfg(chip_px=22, chip_offset_px=4)`
  - `Config(general, privacy, apps:dict[str,AppCfg], ui, raw:dict)` with `app(wm_class) -> AppCfg` (exact casefolded key, else default). `raw` keeps unknown tables (`[backend]`, `[[profile]]`) for Plan 2.
  - `load_config(path:Path) -> Config` — missing file → defaults; unknown keys ignored; wrong types raise `ConfigError(key, reason)`.
  - `class ConfigWatcher(path, clock:Clock, on_change:Callable[[Config],None], on_error:Callable[[ConfigError],None])` with `start()` — polls mtime every 1000 ms; `current: Config`.
- Produces (`log.py`): `log_event(event:str, **fields) -> None` writes one `key=value` line to stderr. Allowed values: `int|float|bool|None`, or `str` matching `^[A-Za-z0-9_.:/+-]{0,64}$`; anything else is written as `<redacted>` (and raises `UnsafeLogValue` when env `GLANCE_STRICT_LOG=1`, which `tests/conftest.py` sets). `fingerprint(text:str) -> str` returns `"len=<n>,sha=<first 8 hex>"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_when_file_missing(tmp_path):
    c = load_config(tmp_path / "nope.toml")
    assert (c.general.chip_idle_ms, c.general.analyze_idle_ms, c.general.min_chars_chip, c.general.min_chars_analyze) == (400, 1200, 15, 25)
    assert c.app("Slack").type_max_chars == 80 and "seahorse" in c.privacy.app_denylist

def test_app_override(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[apps.slack]\nwriteback = "paste"\nundo_steps = 2\n')
    assert load_config(p).app("Slack") == AppCfg(writeback="paste", undo_steps=2)

def test_wrong_type_raises(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[general]\nchip_idle_ms = "soon"\n')
    with pytest.raises(ConfigError): load_config(p)

def test_reload_on_change(tmp_path):
    # write file, start watcher with FakeClock, rewrite with min_chars_chip = 30, bump mtime, clock.advance(1000)
    assert seen[-1].general.min_chars_chip == 30

def test_reload_keeps_last_good_on_invalid_toml(tmp_path):
    # valid file -> watcher.start(); overwrite with '[general\nchip' ; advance(1000) twice
    assert watcher.current.general.chip_idle_ms == 400 and len(errors) == 1   # told once, not every poll

def test_log_event_redacts_free_text(capsys, monkeypatch):
    monkeypatch.delenv("GLANCE_STRICT_LOG"); log_event("x", n=3, wm="slack", t="hello there friend")
    assert "t=<redacted>" in capsys.readouterr().err

def test_log_event_strict_raises():
    with pytest.raises(UnsafeLogValue): log_event("x", t="two words")

def test_fingerprint_has_no_text():
    assert fingerprint("secret words") .startswith("len=12,sha=") and "secret" not in fingerprint("secret words")
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_config.py tests/test_log.py -v` — FAIL.
- [ ] **Step 3: Implement** the three modules and `FakeClock`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: config with hot reload, text-free logging, clock`.

---

### Task 4: Privacy gate and protected entities

**Files:**
- Create: `src/glance/privacy/__init__.py`, `src/glance/privacy/gate.py`, `src/glance/privacy/entities.py`
- Test: `tests/test_gate.py`, `tests/test_entities.py`

**Interfaces:**
- Consumes: `PrivacyCfg`, `Config.app`.
- Produces (`gate.py`):
  - `@dataclass(frozen=True) FieldMeta(app:str, wm_class:str, title:str, url:str|None, role:str, states:frozenset[str], pid:int, text_len:int, terminal_fg:str|None=None)`
  - `@dataclass(frozen=True) GateDecision(allowed:bool, reason:str|None)`; reasons: `password | app_denylist | incognito | url_denylist | url_unknown | terminal | app_disabled | too_long | not_editable`.
  - `class PrivacyGate(cfg:Config)` with `evaluate(meta:FieldMeta) -> GateDecision`, checked in exactly the order listed above. `password`: role is `password text`, or `"protected" in states`, or the role/state strings findings S2.6 records for Chromium and Firefox password inputs. `url_unknown`: wm_class is in `browser_wm_classes` and `url` is `None` or empty. `terminal`: wm_class in `terminal_wm_classes` and `terminal_fg` not in `terminal_allowlist` (default empty ⇒ always gated). `not_editable`: `"editable" not in states`.
- Produces (`entities.py`): `@dataclass(frozen=True) Span(start:int, end:int, kind:str)`; `find_protected(text:str) -> list[Span]` — kinds `object` (each U+FFFC), `url` (`https?://\S+` and `www\.\S+`), `mention` (`(?<!\w)@[\w.-]+`, `<@…>`, `(?<!\w)#[\w-]+`), `emoji` (`:[a-z0-9_+-]+:` and any char in Unicode `So`/emoji ranges), `code` (`` `…` `` and fenced blocks); sorted, non-overlapping (merge overlaps). `overlaps(spans:list[Span], start:int, end:int) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
META = FieldMeta(app="gedit", wm_class="gedit", title="doc", url=None, role="text",
                 states=frozenset({"editable", "focused"}), pid=1, text_len=40)

@pytest.mark.parametrize("change, reason", [
    ({"role": "password text"}, "password"),
    ({"wm_class": "org.remmina.Remmina"}, "app_denylist"),
    ({"app": "Seahorse"}, "app_denylist"),
    ({"wm_class": "AnyDesk"}, "app_denylist"),
    ({"wm_class": "google-chrome", "title": "New Tab - Google Chrome (Incognito)", "url": "https://x.test/"}, "incognito"),
    ({"wm_class": "firefox", "title": "Mozilla Firefox Private Browsing", "url": "https://x.test/"}, "incognito"),
    ({"wm_class": "google-chrome", "url": "https://accounts.google.com/v3/signin"}, "url_denylist"),
    ({"wm_class": "google-chrome", "url": "https://mybank.example/transfer"}, "url_denylist"),
    ({"wm_class": "google-chrome", "url": None}, "url_unknown"),
    ({"wm_class": "com.mitchellh.ghostty"}, "terminal"),
    ({"wm_class": "gnome-terminal-server", "terminal_fg": "zsh"}, "terminal"),
    ({"text_len": 20001}, "too_long"),
    ({"states": frozenset({"focused"})}, "not_editable"),
])
def test_gate_blocks(change, reason):
    assert PrivacyGate(load_config(Path("/nonexistent"))).evaluate(replace(META, **change)) == GateDecision(False, reason)

def test_gate_allows_plain_field_and_slack_without_url():
    g = PrivacyGate(load_config(Path("/nonexistent")))
    assert g.evaluate(META).allowed and g.evaluate(replace(META, wm_class="Slack")).allowed

def test_password_wins_over_everything():
    m = replace(META, role="password text", wm_class="AnyDesk")
    assert PrivacyGate(load_config(Path("/nonexistent"))).evaluate(m).reason == "password"

def test_app_disabled_by_config(tmp_path):  # [apps.gedit] capture = false -> reason "app_disabled"

def test_find_protected_kinds():
    t = "hi @sam see https://x.test/a?b=1 :tada: `rm -rf` ￼ ok 😀"
    assert [(t[s.start:s.end], s.kind) for s in find_protected(t)] == [
        ("@sam", "mention"), ("https://x.test/a?b=1", "url"), (":tada:", "emoji"),
        ("`rm -rf`", "code"), ("￼", "object"), ("😀", "emoji")]

def test_email_address_is_not_a_mention():
    assert find_protected("mail me at a@b.test") == []
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_gate.py tests/test_entities.py -v` — FAIL.
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: privacy gate and protected-entity detection`.

---

### Task 5: Capture — focus tracker, AT-SPI adapter, `glance-capture`

**Files:**
- Create: `src/glance/capture/__init__.py`, `ports.py`, `tracker.py`, `atspi_port.py`, `main.py`; `tests/desktop/gtk_app.py`
- Modify: `tests/fakes.py` (add `FakeField`, `FakeA11y`)
- Test: `tests/test_tracker.py`, `tests/desktop/test_capture_gtk.py`

**Interfaces:**
- Consumes: `PrivacyGate`, `FieldMeta`, `FieldSnapshot`, `Rect`, `Clock`, `JsonLineClient`, `log_event`.
- Produces (`ports.py`):
  - `class A11yTimeout(Exception)`
  - `@dataclass(frozen=True) FieldRead(text:str, caret:int, selection:tuple[int,int]|None, caret_rect:Rect|None, prev_char_rect:Rect|None, field_rect:Rect|None, window_rect:Rect|None)`
  - `class A11yField(Protocol): key:str; meta() -> FieldMeta; read() -> FieldRead; set_selection(start:int, end:int) -> bool; set_caret(offset:int) -> bool` — every method may raise `A11yTimeout`.
  - `class A11yPort(Protocol): start(on_focus:Callable[[A11yField|None],None], on_change:Callable[[A11yField],None]) -> None; focused() -> A11yField|None`
- Produces (`tracker.py`): `class FocusTracker(port:A11yPort, gate:PrivacyGate, emit:Callable[[str,dict],None], clock:Clock, coalesce_ms:int=16)` with `start()`, `current_field() -> A11yField|None`, `set_gate(gate)` (hot reload). Behaviour: on focus or change it schedules one read `coalesce_ms` later (further events inside the window do not add reads); calls `meta()` first, evaluates the gate, and **only if allowed** calls `read()`; emits `SNAPSHOT` with a monotonically increasing `seq`, `field_id = field.key`, `capture_confidence` = `1.0` (caret_rect present) / `0.7` (absent). Gated → `SNAPSHOT` with `gated=<reason>`, `text=""`, `caret=0`, all rects `None` except `window_rect`. Focus to `None` → `FOCUS_LOST {"reason":"blur"}`. `A11yTimeout` → `FOCUS_LOST {"reason":"timeout"}` and no further reads of that field until the next focus event.
- Produces (`atspi_port.py`): `class AtspiPort` implementing `A11yPort` — `Atspi.set_timeout(200, 200)`; listeners for `object:state-changed:focused`, `object:text-changed`, `object:text-caret-moved`; `key = f"{pid}:{Atspi.Accessible.get_id}:{hash of the index path to the application}"`; `wm_class`/`title` of the active window from `Wnck.Screen.get_default().get_active_window()` (`get_class_group_name()`, `get_name()`), `window_rect` from its geometry; `url` from the nearest document ancestor's `DocURL` attribute (name per findings S2.4); GLib errors with a timeout code → `A11yTimeout`.
- Produces (`main.py`): `main()` — builds `ConfigWatcher`, `PrivacyGate`, `AtspiPort`, `FocusTracker`; connects `JsonLineClient(capture_sock())` (reconnect loop every 500 ms on disconnect); handles `APPLY`/`REVERT` by delegating to a `WriteBackEngine` (Task 7; until then replies `ApplyResult(status="failed", detail="writeback unavailable")`); runs `GLib.MainLoop`. `--print` flag prints `SNAPSHOT` metadata (never text: `fingerprint(text)`) to stdout instead of connecting, for debugging.
- Produces (`tests/desktop/gtk_app.py`): a GTK 3 window titled `glance-test-app` with a `Gtk.TextView`, a `Gtk.Entry`, and a `Gtk.Entry(visibility=False, input_purpose=PASSWORD)`; `--text S` pre-fills the TextView; prints `READY` on map.

- [ ] **Step 1: Write the failing tests (`tests/test_tracker.py`)** using `FakeA11y`/`FakeField` (settable `text`, `caret`, `meta`, `raise_timeout`, and a `reads` counter):

```python
def test_emits_snapshot_after_coalesce_window():
    # focus field(text="hello world", caret=5); clock.advance(16)
    t, p = out[-1]; s = FieldSnapshot.from_dict(p)
    assert t == SNAPSHOT and (s.text, s.caret, s.field_id, s.gated) == ("hello world", 5, field.key, None)

def test_burst_of_changes_is_one_read():
    # 10 change events within 10 ms, advance(16)
    assert field.reads == 1 and len(out) == 1

def test_seq_increases():            # two changes 20 ms apart -> seq 1 then 2
def test_password_field_text_is_never_read():
    # meta role "password text"; advance
    s = FieldSnapshot.from_dict(out[-1][1])
    assert field.reads == 0 and s.gated == "password" and s.text == ""

def test_blur_emits_focus_lost():    # on_focus(None) -> (FOCUS_LOST, {"reason": "blur"})
def test_timeout_emits_focus_lost_and_stops_reading():
    # field.raise_timeout = True; change; advance -> FOCUS_LOST reason "timeout"; another change -> reads unchanged
def test_missing_caret_rect_lowers_confidence():   # caret_rect None -> capture_confidence == 0.7
def test_gate_swap_applies_to_next_event():        # set_gate(gate denying "gedit") -> next snapshot gated == "app_disabled"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_tracker.py -v` — FAIL.
- [ ] **Step 3: Implement** `ports.py`, `tracker.py`, the fakes.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Write the desktop test (`tests/desktop/test_capture_gtk.py`, `@pytest.mark.desktop`)** — fixture starts `gtk_app.py --text "Teh quick brown fox"` and `glance-capture --print --with-text-for-tests` (flag honoured only when env `GLANCE_TEST=1`; prints full snapshots), waits for `READY`, focuses the window with `Wnck`. `test_gtk_textview_snapshot`: a snapshot arrives within 2 s with `text == "Teh quick brown fox"`, `caret_rect.h > 0`, `wm_class` containing `gtk_app`. `test_gtk_password_entry_gated`: after Tab ×2, last snapshot has `gated == "password"` and `text == ""`.
- [ ] **Step 6: Implement** `atspi_port.py`, `main.py`, `gtk_app.py`. Add script entry already declared in Task 1.
- [ ] **Step 7: Run** `.venv/bin/pytest -m desktop tests/desktop/test_capture_gtk.py -v` in the GNOME session — PASS. Manual check: `glance-capture --print` while typing in Chrome started with `--force-renderer-accessibility` shows `len=` tracking keystrokes and `gated=incognito` in an incognito window.
- [ ] **Step 8: Commit** `feat: at-spi capture process with hard privacy gate`.

---

### Task 6: Harper client, issue model, dictionary and ignore rules

**Files:**
- Create: `scripts/fetch-harper.sh`, `src/glance/grammar/__init__.py`, `issues.py`, `harper.py`, `store.py`, `filters.py`
- Test: `tests/test_issues.py`, `tests/test_harper_protocol.py`, `tests/test_filters.py`, `tests/test_harper_live.py`

**Interfaces:**
- Consumes: `Edit`, `find_protected`, `Span`, `overlaps`, `paths.*`.
- Produces (`issues.py`):
  - `@dataclass(frozen=True) Issue(id:str, source:str, rule:str, message:str, start:int, end:int, replacements:tuple[str,...], safe:bool)` — `source` ∈ `harper|llm`; `id = f"{source}:{rule}:{start}:{end}"`; `safe` is true only when there is exactly one replacement and `rule != "SpellCheck"`.
  - `utf16_to_cp(text:str, line:int, character:int) -> int` — LSP position → code-point offset into `text`.
  - `issue_edit(issue:Issue, choice:int=0) -> Edit`
  - `merge_edits(text:str, edits:list[Edit]) -> Edit` — the single minimal contiguous `Edit` equivalent to applying all (non-overlapping) edits; raises `ValueError` on overlap.
  - `apply_edit(text:str, edit:Edit) -> str`
- Produces (`harper.py`): `class HarperClient(binary:Path, user_dict:Path, timeout_s:float=0.5)` with `start() -> None`, `check(text:str) -> list[Issue]` (thread-safe; `didChange` full-text sync with incrementing version, waits for the matching `publishDiagnostics`, then one `textDocument/codeAction` per diagnostic for replacements; returns `[]` and sets `degraded=True` on timeout), `set_disabled_rules(rules:set[str])`, `reload_dictionary()`, `alive:bool`, `stop()`. Pure helpers kept separate for testing: `frame(msg:dict) -> bytes`, `class FrameReader(feed(data:bytes) -> list[dict])`, `diagnostics_to_issues(text:str, diagnostics:list[dict], actions:dict[int,list[dict]]) -> list[Issue]`. Rule name = diagnostic `code`; settings shape per findings S7.5.
- Produces (`store.py`): `class PersonalDictionary(path:Path)`: `add(word)`, `words() -> frozenset[str]`, `contains(word) -> bool` (casefolded); one word per line. `class IgnoredRules(path:Path)`: `ignore(rule:str, wm_class:str|None)`, `is_ignored(rule, wm_class) -> bool`, `global_rules() -> set[str]`; JSON `{"global":[…], "apps":{wm_class:[…]}}`. Both write atomically (temp file in the same dir + `os.replace`) and contain only words/rule names.
- Produces (`filters.py`): `SpanFilter = Callable[[str], list[Span]]`; `class IssueFilter(dictionary, ignored, span_filters:list[SpanFilter])` with `apply(text:str, issues:list[Issue], wm_class:str) -> list[Issue]` — drops issues that overlap any protected span (`find_protected` is always in the list), are ignored for the app or globally, or whose flagged word is in the dictionary. `span_filters` is the seam Plan 3 uses for Banglish spans.
- `scripts/fetch-harper.sh`: downloads the `harper-ls` version and verifies the SHA-256 recorded in findings S7.3 into `~/.local/share/glance/bin/harper-ls`; exits non-zero on checksum mismatch.

- [ ] **Step 1: Write the failing tests**

```python
def test_utf16_columns_map_to_codepoints():
    text = "I 😀 recieve it"            # 😀 is 2 UTF-16 units, 1 code point
    assert utf16_to_cp(text, 0, 5) == 4 and text[4:11] == "recieve"

def test_utf16_second_line():
    assert utf16_to_cp("ab\ncd", 1, 1) == 4

def test_merge_edits_minimal_span():
    t = "teh cat sat on teh mat"
    e = merge_edits(t, [Edit(0, 3, "the"), Edit(15, 18, "the")])
    assert e == Edit(0, 18, "the cat sat on the") and apply_edit(t, e) == "the cat sat on the mat"

def test_merge_edits_rejects_overlap():
    with pytest.raises(ValueError): merge_edits("abcdef", [Edit(0, 3, "x"), Edit(2, 4, "y")])

def test_safe_flag():
    assert Issue("i", "harper", "SentenceCapitalization", "m", 0, 1, ("T",), True).safe
    # diagnostics_to_issues gives safe=False for rule "SpellCheck" and for 2+ replacements

def test_frame_reader_handles_split_and_joined_frames():
    r = FrameReader(); a, b = frame({"id": 1}), frame({"id": 2})
    assert r.feed(a[:5]) == [] and r.feed(a[5:] + b) == [{"id": 1}, {"id": 2}]

def test_diagnostics_to_issues_uses_codepoints():
    # diagnostic range (0,5)-(0,12), code "SpellCheck", action replacing with "receive", text "I 😀 recieve it"
    assert (i.start, i.end, i.rule, i.replacements, i.safe) == (4, 11, "SpellCheck", ("receive",), False)

def test_filter_drops_issue_inside_mention_url_code():
    # issues over "@jhon", "https://exmaple.test", "`teh`" are dropped; one over "teh" in prose is kept
def test_filter_drops_dictionary_word_and_ignored_rule(tmp_path):
    # dictionary.add("Banglish"); ignored.ignore("LongSentences", "slack")
    # -> SpellCheck issue on "banglish" dropped; LongSentences dropped for wm_class "Slack", kept for "gedit"
def test_extra_span_filter_suppresses(tmp_path):
    f = IssueFilter(d, ig, [lambda t: [Span(0, len(t), "lang")]]); assert f.apply("teh", [issue], "gedit") == []
def test_dictionary_file_contains_only_words(tmp_path):
    d.add("Zebulon"); assert (tmp_path / "dictionary.txt").read_text() == "Zebulon\n"
```

`tests/test_harper_live.py` (skipped with `pytest.skip` when `harper_bin()` is missing): `test_live_check_finds_misspelling` — `check("Teh quick brown fox.")` returns an issue with `(start, end) == (0, 3)` and `"The" in replacements`; `test_live_check_under_150ms` — median of 20 `check` calls on a 500-char text < 0.15 s; `test_killed_harper_reports_not_alive` — kill the child, `check` returns `[]`, `alive is False`.

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_issues.py tests/test_harper_protocol.py tests/test_filters.py -v` — FAIL.
- [ ] **Step 3: Implement** the four modules and the fetch script; run `scripts/fetch-harper.sh`.
- [ ] **Step 4: Run** the three files plus `tests/test_harper_live.py` — PASS, live tests not skipped.
- [ ] **Step 5: Commit** `feat: harper client, issue model, dictionary and ignore rules`.

---

### Task 7: Write-back engine with verify and revert

**Files:**
- Create: `src/glance/writeback/__init__.py`, `ports.py`, `engine.py`, `keys_xlib.py`, `clipboard_gtk.py`
- Modify: `src/glance/capture/main.py` (wire the engine), `tests/fakes.py` (add `FakeKeys`, `FakeClipboard`, make `FakeField` editable by them)
- Test: `tests/test_writeback.py`, `tests/desktop/test_writeback_gtk.py`

**Interfaces:**
- Consumes: `A11yField`, `ApplyRequest`, `ApplyResult`, `Edit`, `apply_edit`, `text_sha256`, `AppCfg`, `find_protected`, `overlaps`, `log_event`.
- Produces (`ports.py`):
  - `class KeyPort(Protocol): can_type(text:str) -> bool; type_text(text:str, delay_ms:int) -> None; chord(*keys:str) -> None` — key names `ctrl, shift, alt, super, v, z, Left, Right, …`.
  - `class ClipboardPort(Protocol): save() -> ClipSnapshot|None; set_secret_text(text:str) -> None; restore(snap:ClipSnapshot) -> None` — `save()` returns `None` when the current content cannot be restored faithfully (non-text targets over 8 MB, or an owner that will not hand over its data); `set_secret_text` advertises `x-kde-passwordManagerHint = secret`.
- Produces (`engine.py`): `class WriteBackEngine(field:Callable[[],A11yField|None], keys:KeyPort, clip:ClipboardPort, app_cfg:Callable[[str],AppCfg], sleep:Callable[[float],None])` with `apply(req:ApplyRequest) -> ApplyResult` and `revert_last() -> ApplyResult`. Algorithm for `apply` (the order is the contract):
  1. No focused field, or `field.key != req.field_id` → `stale`.
  2. `cfg.writeback == "off"` → `refused` ("writeback off").
  3. `read()`; `text_sha256(text) != req.expected_sha256` → `stale`. **No key or clipboard call has happened yet.**
  4. Edit range overlaps a protected span of the current text (unless the replacement keeps every protected substring verbatim and in order) → `refused` ("protected").
  5. Select `[start, end)`: `set_selection`; if it returns False or `cfg.select == "keys"`: `set_caret(end)` then `chord("shift","Left")` × (end−start). Empty range → `set_caret(start)` only.
  6. Insert: `type` when method is `type`, or `auto` with `len(replacement) <= cfg.type_max_chars and keys.can_type(replacement)`; otherwise paste — `snap = clip.save()`; if `snap is None` → collapse the selection (`set_caret(end)`) and return `refused` ("clipboard not restorable"); else `set_secret_text`, `chord("ctrl","v")`, `sleep(0.05)`, `restore(snap)`. An empty replacement uses `chord("BackSpace")`.
  7. `sleep(0.15)`; `read()`; compare `normalize(text)` to `normalize(apply_edit(old, edit))` where `normalize` = NFC + U+00A0→space + strip one trailing `\n`. Equal → `verified`; remember `(field_id, before, after, cfg.undo_steps)` as last apply.
  8. Mismatch → `chord("ctrl","z")` × `cfg.undo_steps`, `sleep(0.15)`, `read()`; text equals the original → `reverted`; else `failed` ("could not restore; use Copy instead"). After `failed` the engine sends nothing further.
  - `revert_last()`: only when the focused field is the last-apply field and its current text hash equals the remembered `after` → Ctrl+Z × undo_steps, verify equals `before` → `reverted`; otherwise `stale`. Clears the memory either way.
  - Any `A11yTimeout` → `failed` ("target not responding") with no further keys.
- Produces (`keys_xlib.py`): `XlibKeys` (XTEST `fake_input`; `can_type` true only if every char maps to a keycode in the current keymap without remapping). (`clipboard_gtk.py`): `GtkClipboard` over `Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)`; saves text, `text/html` and image targets up to 8 MB.

- [ ] **Step 1: Write the failing tests** — `FakeField` models an editor: `FakeKeys.type_text`/paste replace the current selection, `chord("ctrl","z")` restores the previous text; knobs `mangle` (editor alters inserted text), `selection_fails`, `undo_broken`.

```python
def req(field, edit, method="auto"): return ApplyRequest("a1", field.key, text_sha256(field.text), edit, method)

def test_short_fix_is_typed_and_verified():
    f = FakeField("Teh cat"); r = eng(f).apply(req(f, Edit(0, 3, "The")))
    assert r.status == "verified" and f.text == "The cat" and keys.typed == ["The"] and clip.calls == []

def test_long_fix_is_pasted_and_clipboard_restored():
    clip.content = "CLIP-ORIGINAL"; new = "x" * 81
    assert eng(f).apply(req(f, Edit(0, 3, new))).status == "verified"
    assert clip.content == "CLIP-ORIGINAL" and clip.secret_hint_used and keys.chords.count(("ctrl", "v")) == 1

def test_apply_is_stale_when_text_changed():
    f = FakeField("Teh cat"); r0 = req(f, Edit(0, 3, "The")); f.text = "Teh cats"
    assert eng(f).apply(r0).status == "stale" and keys.events == [] and clip.calls == []

def test_stale_when_focus_moved():           # field() returns a different key -> "stale", no events
def test_mismatch_triggers_single_undo_and_reverted():
    f = FakeField("Teh cat", mangle=lambda s: s.upper())
    assert eng(f).apply(req(f, Edit(0, 3, "The"))).status == "reverted" and f.text == "Teh cat"
    assert keys.chords.count(("ctrl", "z")) == 1

def test_undo_steps_from_app_config():       # AppCfg(undo_steps=2) + mangle -> two ctrl+z
def test_failed_when_undo_does_not_restore():
    f = FakeField("Teh cat", mangle=str.upper, undo_broken=True); n = len(keys.events)
    r = eng(f).apply(req(f, Edit(0, 3, "The"))); assert r.status == "failed" and "Copy" in r.detail

def test_paste_refused_when_clipboard_not_restorable():
    clip.unsaveable = True      # e.g. an image the owner will not hand over
    r = eng(f).apply(req(f, Edit(0, 3, "x" * 81)))
    assert r.status == "refused" and f.text == "Teh cat" and ("ctrl", "v") not in keys.chords

def test_untypeable_short_text_goes_to_paste():   # keys.can_type -> False for "naïve —" -> paste path
def test_selection_fallback_uses_shift_left():    # selection_fails -> ("shift","Left") x 3
def test_refuses_edit_touching_mention():
    f = FakeField("hi @jhon teh"); assert eng(f).apply(req(f, Edit(3, 8, "@john"))).status == "refused"
def test_nbsp_and_trailing_newline_tolerated():   # editor returns "The cat\n" for "The cat" -> verified
def test_writeback_off():                         # AppCfg(writeback="off") -> refused, no events
def test_timeout_is_failed_without_keys():        # field.raise_timeout -> failed, keys.events == []
def test_revert_last_restores_and_is_single_use():
    # apply verified -> revert_last() == reverted, text original; second revert_last() -> stale
def test_revert_last_stale_after_user_typed():    # user edits after apply -> stale, no ctrl+z
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_writeback.py -v` — FAIL.
- [ ] **Step 3: Implement** `ports.py`, `engine.py`, fakes.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Desktop test (`@pytest.mark.desktop`)** against `gtk_app.py --text "Teh quick brown fox jump over the lazy dog"`: `test_type_fix_in_textview` (Edit(0,3,"The") → app text starts `The quick`), `test_paste_fix_restores_clipboard` (clipboard pre-set to `CLIP-ORIGINAL`, 120-char replacement, clipboard equals `CLIP-ORIGINAL` after), `test_single_ctrl_z_restores` (XTEST Ctrl+Z once → original text). The app prints its buffer on `SIGUSR1` so the test reads ground truth without AT-SPI.
- [ ] **Step 6: Implement** `keys_xlib.py`, `clipboard_gtk.py`; wire `WriteBackEngine` into `capture/main.py` for `APPLY` and `REVERT` (reply `APPLY_RESULT`).
- [ ] **Step 7: Run** `.venv/bin/pytest -m desktop tests/desktop/test_writeback_gtk.py -v` — PASS.
- [ ] **Step 8: Commit** `feat: write-back with verify, auto-revert and revert-last`.

---

### Task 8: Chip model, placement, check scheduler

**Files:**
- Create: `src/glance/ui/__init__.py`, `chip_model.py`, `placement.py`, `src/glance/daemon/__init__.py`, `scheduler.py`
- Test: `tests/test_chip_model.py`, `tests/test_placement.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `FieldSnapshot`, `Rect`, `Clock`, `GeneralCfg`, `UiCfg`.
- Produces (`chip_model.py`):
  - `class ChipState(Enum)`: `HIDDEN, IDLE, FIXES, SPINNER, TONE, PAUSED, PRIVATE, AI_LIMIT, NO_AI, GUARD` (the last five beyond `PRIVATE` are produced from Plan 2/3 inputs; the enum is complete now so the overlay draws all of them).
  - `@dataclass(frozen=True) ChipInputs(has_field:bool=False, paused:bool=False, gated:str|None=None, text_len:int=0, settled:bool=False, snoozed:bool=False, n_local:int=0, analyzing:bool=False, tone_label:str|None=None, n_llm:int=0, mismatch:bool=False, ai:str="off", guard:str|None=None)` — `ai` ∈ `off|ok|none|limit`.
  - `@dataclass(frozen=True) ChipView(state:ChipState, label:str, badge:str|None)`
  - `chip_view(i:ChipInputs, min_chars:int=15) -> ChipView` — priority, first match wins: no field → HIDDEN; paused → PAUSED "Paused"; gated → PRIVATE "Private"; not settled, or `text_len < min_chars`, or snoozed → HIDDEN; guard → GUARD; `mismatch` → TONE `f"{tone_label} · {n_local + n_llm}"` (count omitted when 0); analyzing → SPINNER; `n_local + n_llm > 0` → FIXES `str(n)`; `ai == "limit"` → AI_LIMIT "AI paused (limit)"; `ai == "none"` → NO_AI "Connect an AI"; else IDLE. For FIXES the `badge` carries the AI condition (`"limit"`/`"none"`) so a count is never replaced by a nag.
- Produces (`placement.py`): `place(caret:Rect|None, prev_char:Rect|None, field:Rect|None, window:Rect|None, size:tuple[int,int], monitors:list[Rect], offset:int=4) -> tuple[int,int]|None` — a rect is *usable* when `w >= 0`, `h > 0`, `h <= 200` and its centre lies inside some monitor. Order: caret → below-right of it (`x+w+offset`, `y+h+offset`); previous char → same from its right edge; field → inside its bottom-right corner; window → inside its bottom-right corner; none → `None`. The result is clamped fully inside the monitor containing the anchor's centre.
- Produces (`scheduler.py`): `class CheckScheduler(clock:Clock, cfg:Callable[[],GeneralCfg], on_local:Callable[[FieldSnapshot,int],None], on_settled:Callable[[FieldSnapshot,int],None], on_analyze:Callable[[FieldSnapshot,int],None], on_typing:Callable[[],None])` with `on_snapshot(s)`, `on_focus_lost()`, `generation:int`. Each text-changing snapshot bumps `generation`, calls `on_typing()` at once (hide the chip), calls `on_local` after 80 ms of quiet, `on_settled` after `chip_idle_ms`, and `on_analyze` after `analyze_idle_ms` when `len(text) >= min_chars_analyze` and the change is *meaningful* versus the last analyzed text of that `field_id`: the multisets of whitespace-separated casefolded tokens differ by ≥ 2 tokens, or no analysis exists yet. Caret-only snapshots (same text) re-fire `on_settled` after 50 ms and nothing else. Gated snapshots and focus loss cancel all timers and bump `generation`.

- [ ] **Step 1: Write the failing tests**

```python
def test_chip_priority():
    V = lambda **k: chip_view(ChipInputs(has_field=True, settled=True, text_len=40, **k))
    assert chip_view(ChipInputs()).state is ChipState.HIDDEN
    assert V(paused=True, n_local=3).state is ChipState.PAUSED
    assert V(gated="password").state is ChipState.PRIVATE
    assert V(settled=False, n_local=3).state is ChipState.HIDDEN          # never while typing
    assert V(text_len=14, n_local=1).state is ChipState.HIDDEN
    assert V(snoozed=True, n_local=1).state is ChipState.HIDDEN
    assert V(n_local=3) == ChipView(ChipState.FIXES, "3", None)
    assert V(n_local=2, ai="none") == ChipView(ChipState.FIXES, "2", "none")
    assert V(ai="none").state is ChipState.NO_AI and V(ai="limit").label == "AI paused (limit)"
    assert V(mismatch=True, tone_label="Blunt", n_local=1, n_llm=2).label == "Blunt · 3"
    assert V(analyzing=True).state is ChipState.SPINNER and V().state is ChipState.IDLE

MON = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
def test_below_right_of_caret():
    assert place(Rect(100, 100, 1, 18), None, None, None, (22, 22), MON) == (105, 122)
def test_clamped_to_caret_monitor():
    assert place(Rect(1915, 1070, 1, 18), None, None, None, (22, 22), MON) == (1898, 1058)
def test_second_monitor_stays_on_second():
    x, y = place(Rect(1925, 500, 1, 18), None, None, None, (22, 22), MON); assert x >= 1920
def test_garbage_caret_rect_falls_back():
    for bad in (Rect(0, 0, 0, 0), Rect(-1, -1, 0, 0), Rect(50, 50, 1, 5000), Rect(9000, 9000, 1, 18)):
        assert place(bad, None, Rect(200, 200, 400, 100), None, (22, 22), MON) == (574, 274)
def test_fallback_order_prev_char_then_field_then_window_then_none():
    assert place(None, Rect(100, 100, 8, 18), None, None, (22, 22), MON) == (112, 122)
    assert place(None, None, None, Rect(0, 0, 800, 600), (22, 22), MON) == (774, 574)
    assert place(None, None, None, None, (22, 22), MON) is None
def test_negative_origin_monitor():
    mons = [Rect(-1920, 0, 1920, 1080), Rect(0, 0, 1920, 1080)]
    assert place(Rect(-100, 100, 1, 18), None, None, None, (22, 22), mons) == (-95, 122)

def test_scheduler_timeline():
    # snapshot(40 chars) at t=0 -> on_typing immediately; advance(80) -> on_local; advance to 400 -> on_settled;
    # advance to 1200 -> on_analyze; all with the same generation
def test_typing_restarts_all_timers_and_bumps_generation():
    # snapshots every 100 ms x5 -> no on_settled / on_analyze until quiet; generation == 5
def test_no_analyze_under_25_chars():            # 24-char text -> on_analyze never called
def test_no_analyze_for_trivial_change():        # analyzed "… fox"; new text differs by 1 token -> not called; by 2 -> called
def test_caret_move_only_resettles():            # same text, new caret -> on_settled after 50 ms, no on_local/on_analyze, generation unchanged
def test_focus_lost_cancels_everything():        # snapshot; on_focus_lost(); advance(5000) -> nothing fired
def test_idle_change_uses_live_config():         # cfg() returns chip_idle_ms=1000 after reload -> settle at 1000
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_chip_model.py tests/test_placement.py tests/test_scheduler.py -v` — FAIL.
- [ ] **Step 3: Implement** the three modules.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: chip state model, placement, check scheduler`.

---

### Task 9: Controller and panel model (the daemon's brain, no GTK)

**Files:**
- Create: `src/glance/ui/panel_model.py`, `src/glance/ui/ports.py`, `src/glance/daemon/controller.py`
- Modify: `tests/fakes.py` (add `FakeOverlay`, `FakeCapture`, `FakeNotifier`, `FakeTray`, `FakeHarper`)
- Test: `tests/test_controller.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces (`ui/ports.py`):
  - `class OverlayPort(Protocol): show_chip(view:ChipView, x:int, y:int) -> None; hide_chip() -> None; show_panel(view:PanelView, x:int, y:int) -> None; hide_panel() -> None; monitors() -> list[Rect]; copy_to_clipboard(text:str) -> None`
  - `class CapturePort(Protocol): send_apply(req:ApplyRequest) -> None; send_revert() -> None; connected:bool`
  - `class NotifierPort(Protocol): notify(summary:str, body:str) -> None`
  - `class TrayPort(Protocol): set_state(state:str) -> None` — `active|paused|private|degraded|no_ai`.
  - `class GrammarPort(Protocol): check(text:str) -> list[Issue]; alive:bool`
- Produces (`panel_model.py`): `@dataclass(frozen=True) IssueRow(issue_id:str, excerpt:str, message:str, replacements:tuple[str,...], rule:str)`; `ToneChip(key:str, label:str, hotkey:int|None)`; `PanelView(tone:str|None, tone_reason:str|None, tone_chips:tuple[ToneChip,...], issues:tuple[IssueRow,...], banner:str|None, can_fix_all:bool, copy_offer:str|None)`; `build_panel(text:str, issues:list[Issue], …) -> PanelView` (`excerpt` = flagged text with ≤ 20 chars of context each side). Tone fields stay empty in this plan.
- Produces (`controller.py`): `class Controller(clock, cfg:Callable[[],Config], grammar:GrammarPort, issue_filter:IssueFilter, overlay:OverlayPort, capture:CapturePort, notifier:NotifierPort, tray:TrayPort, run_bg:Callable[[Callable[[],T], Callable[[T],None]],None])` — `run_bg(work, done)` runs `work` off the main loop and calls `done(result)` back on it (tests pass a synchronous version). Methods (all called on the main loop):
  - `handle_snapshot(s:FieldSnapshot)`, `handle_focus_lost(reason:str)`, `handle_apply_result(r:ApplyResult)`, `handle_capture_disconnected()`
  - `open_panel()`, `close_panel(snooze:bool=False)`, `accept_issue(issue_id:str, choice:int=0)`, `fix_all_safe()`, `revert_last()`
  - `add_to_dictionary(issue_id)`, `ignore_rule(issue_id, scope:str)` — scope `app|global`
  - `toggle_pause(seconds:int|None=None)`, `resume()`, `status() -> dict` (`{"paused","pause_remaining_s","field":{"wm_class","gated","len"}|None,"local_issues","capture_connected","harper_alive"}` — no text)
  - Extension points for Plan 2/3: attribute `scheduler:CheckScheduler`; hooks `on_analyze_due:Callable[[FieldSnapshot,int],None]|None`, `extra_chip_inputs:Callable[[FieldSnapshot],dict]` (merged into `ChipInputs`), `extra_panel:Callable[[FieldSnapshot,PanelView],PanelView]`, `extra_status:list[Callable[[],dict]]` (each dict merged into `status()`; values must be numbers, bools or short enum strings); method `request_apply(edit:Edit, method:str="auto") -> str` (returns apply_id; used by tone rewrites), `current:FieldSnapshot|None`, `generation:int`.
- Rules the tests pin: Harper results are dropped when their generation is stale; an apply is sent with `expected_sha256` of the snapshot the issue was computed on; while an apply is pending no second apply is sent; `fix_all_safe` sends one `merge_edits` of all `safe` issues (when the merged span would cross a protected entity it sends nothing and the panel banner says `"Fix individually — text between fixes is protected"`); results: `verified` → issues recomputed on the next snapshot; `reverted` → notify `"Glance undid a change"`/`"The app changed the text differently. Nothing was lost."`; `failed` and `refused` → panel `copy_offer` = the replacement text and notify once; `stale` → silent. Gated or paused → chip PRIVATE/PAUSED, panel closed, grammar not called. Timed pause resumes by `clock.call_later`. Capture disconnect → chip hidden, tray `degraded`.

- [ ] **Step 1: Write the failing tests**

```python
def test_local_issue_shows_fixes_chip_after_settle():
    # FakeHarper returns one issue for "Teh"; snapshot("Teh quick brown fox jumps"); advance(400)
    assert overlay.chip.state is ChipState.FIXES and overlay.chip.label == "1" and overlay.chip_xy is not None

def test_chip_hidden_while_typing():
    # chip visible; new snapshot -> overlay.chip is None immediately; visible again only after 400 ms quiet
def test_stale_harper_result_dropped():
    # run_bg deferred: result for generation 1 delivered after snapshot 2 -> issues reflect snapshot 2 only
def test_gated_snapshot_never_reaches_harper_and_closes_panel():
    c.open_panel(); c.handle_snapshot(snap(gated="password", text=""))
    assert harper.calls == n_before and overlay.panel is None and overlay.chip.state is ChipState.PRIVATE
    assert tray.state == "private"

def test_accept_issue_sends_apply_with_snapshot_hash():
    c.accept_issue(issue.id); r = capture.applies[-1]
    assert (r.edit, r.expected_sha256, r.field_id) == (Edit(0, 3, "The"), text_sha256(s.text), s.field_id)

def test_no_second_apply_while_one_pending():   # accept twice quickly -> len(capture.applies) == 1
def test_fix_all_safe_sends_one_merged_edit_and_skips_unsafe():
    # issues: safe(0,3,"The"), unsafe spelling(10,15), safe(20,23,"the") -> one apply == merge of the two safe
def test_fix_all_refuses_to_span_a_mention():
    # safe issues either side of "@sam" -> no apply; overlay.panel.banner startswith "Fix individually"
def test_reverted_result_notifies_user():       # handle_apply_result(reverted) -> notifier.calls[-1][0] == "Glance undid a change"
def test_failed_result_offers_copy():
    c.handle_apply_result(ApplyResult(aid, "failed", "…")); assert overlay.panel.copy_offer == "The"
def test_pause_and_timed_resume():
    c.toggle_pause(1800); assert c.status()["paused"] and tray.state == "paused" and overlay.chip.state is ChipState.PAUSED
    clock.advance(1_800_000); assert not c.status()["paused"]
def test_paused_does_not_call_harper():
def test_snooze_hides_chip_until_field_changes():   # close_panel(snooze=True) -> hidden; new field_id -> visible again
def test_add_to_dictionary_removes_issue_and_persists(tmp_path):
def test_ignore_rule_for_app_only(tmp_path):
def test_capture_disconnect_degrades():         # handle_capture_disconnected() -> chip None, tray.state == "degraded"
def test_harper_dead_shows_degraded_not_crash():    # harper.alive False -> tray "degraded", status()["harper_alive"] is False
def test_status_contains_no_text():
    assert "quick" not in json.dumps(c.status())
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_controller.py -v` — FAIL.
- [ ] **Step 3: Implement** `ui/ports.py`, `panel_model.py`, `controller.py`, fakes.
- [ ] **Step 4: Run** — PASS; full suite `.venv/bin/pytest` — PASS.
- [ ] **Step 5: Commit** `feat: daemon controller and panel model`.

---

### Task 10: GTK overlay, hotkeys, tray, `glanced`, `glance` CLI — end to end

**Files:**
- Create: `src/glance/ui/hotkeys.py`, `hotkeys_xlib.py`, `overlay_gtk.py`, `tray_gtk.py`, `src/glance/daemon/supervisor.py`, `src/glance/daemon/main.py`, `src/glance/cli.py`
- Test: `tests/test_hotkeys.py`, `tests/test_supervisor.py`, `tests/test_cli.py`, `tests/desktop/test_overlay.py`, `tests/desktop/test_end_to_end.py`

**Interfaces:**
- Consumes: `Controller`, ports, `JsonLineServer`, `HarperClient`, `ConfigWatcher`, `GLibClock`.
- Produces (`hotkeys.py`): `parse_chord(s:str) -> tuple[frozenset[str], str]` (`"Super+Alt+space"` → `({"super","alt"}, "space")`; raises `ValueError` on unknown modifier or empty key); `class HotkeyPort(Protocol): grab(name:str, chord:str, cb:Callable[[],None]) -> bool; ungrab(name:str) -> None; grabbed() -> dict[str,str]`. (`hotkeys_xlib.py`): `XlibHotkeys(post)` — `XGrabKey` on the root for the four lock-mask variants, own Xlib connection on a thread, callbacks via `post`; `grab` returns False (and logs) on `BadAccess`.
- Produces (`overlay_gtk.py`): `class GtkOverlay(on_chip_click, on_panel_action:Callable[[str,dict],None])` implementing `OverlayPort`. Chip: `Gtk.Window(type=POPUP)`, 22×22 (wider for labels), `set_accept_focus(False)`, RGBA, cairo-drawn per `ChipState`. Panel: POPUP window, never focused, max 420×480, rows for issues with buttons emitting actions `accept{issue_id,choice}`, `fix_all`, `add_dict{issue_id}`, `ignore{issue_id,scope}`, `copy`, `close`. Both clamp to the monitor via `placement.place`.
- Produces (`tray_gtk.py`): `class GtkTray(on_pause, on_resume, on_quit)` implementing `TrayPort` with `AyatanaAppIndicator3`; when the GIR is missing it becomes a no-op and logs `tray_unavailable` once (the package `gir1.2-ayatanaappindicator3-0.1` is installed by Plan 4's installer; tell the user to run `sudo apt install gir1.2-ayatanaappindicator3-0.1` now to see the tray).
- Produces (`supervisor.py`): `class ChildSupervisor(name:str, spawn:Callable[[],Popen], clock:Clock, on_state:Callable[[str],None])` with `start()`, `stop()`, `poll()` (called every 500 ms) — restarts a dead child after backoff 0.2 s, 0.5 s, 1 s, 2 s (capped), resets the backoff after 60 s alive, reports `up|restarting|failed` (failed after 5 deaths in 60 s; keeps trying every 30 s).
- Produces (`daemon/main.py`): `main()` — single instance (exits 1 with a message if `ctl.sock` answers `ping`); starts `JsonLineServer(capture_sock())` and `JsonLineServer(ctl_sock())`, `ChildSupervisor` for `glance-capture`, `HarperClient` (restarted through a supervisor on death), `GtkOverlay`, `XlibHotkeys` (panel, fix_all, revert, pause; `Escape` grabbed only between `show_panel` and `hide_panel` → `close_panel(snooze=True)`), `GtkTray`, `Controller`; `Gtk.main()`. Control messages on `ctl.sock`: `status`, `pause {"seconds":int|None}`, `resume`, `ping` → reply dict with the same `id`.
- Produces (`cli.py`): `main(argv=None) -> int`; `glance status | pause [DURATION] | resume`; `parse_duration(s:str) -> int` (`"30m"`→1800, `"2h"`→7200, `"45s"`→45, bare int = minutes; raises `ValueError`). Daemon not running → prints `glanced is not running` to stderr, exit 3.

- [ ] **Step 1: Write the failing unit tests**

```python
def test_parse_chord():
    assert parse_chord("Super+Alt+space") == (frozenset({"super", "alt"}), "space")
    with pytest.raises(ValueError): parse_chord("Hyper+x")
def test_parse_duration():
    assert [parse_duration(x) for x in ("30m", "2h", "45s", "10")] == [1800, 7200, 45, 600]
def test_cli_status_when_daemon_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path)); assert main(["status"]) == 3
def test_cli_pause_sends_seconds(tmp_path, monkeypatch):   # fake ctl server records ("pause", {"seconds": 1800})
def test_supervisor_restarts_with_backoff():
    # child dies immediately each time: spawn times at 0, 200, 700, 1700, 3700 ms (FakeClock)
def test_supervisor_reports_failed_after_5_deaths_in_60s():
def test_supervisor_backoff_resets_after_60s_alive():
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_hotkeys.py tests/test_supervisor.py tests/test_cli.py -v` — FAIL.
- [ ] **Step 3: Implement** `hotkeys.py`, `supervisor.py`, `cli.py`. Run — PASS.
- [ ] **Step 4: Write the desktop tests (`@pytest.mark.desktop`)**
  - `tests/desktop/test_overlay.py`: `test_chip_does_not_take_focus` — focus `gtk_app.py`, `show_chip` at its caret, assert `Wnck` active window is still `glance-test-app`; `test_panel_does_not_take_focus` likewise; `test_chip_reposition_under_50ms` — 100 `show_chip` moves, median wall time until `window.get_position()` matches < 0.05 s.
  - `tests/desktop/test_end_to_end.py`: starts `glanced` with `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` pointed at tmp dirs (harper binary symlinked in) and `gtk_app.py --text "Teh quick brown fox jumps over the lazy dog."`. `test_fix_all_hotkey_fixes_text`: wait until `glance status` reports `local_issues >= 1`; send Super+Alt+F with XTEST; app buffer (via `SIGUSR1`) starts with `The quick` within 2 s. `test_revert_hotkey_restores`: then Super+Alt+Z → buffer starts with `Teh quick`. `test_pause_cli`: `glance pause 1m` → `status` shows `paused: true`. `test_capture_killed_restarts_within_2s`: `pkill -f glance-capture`; `status.capture_connected` is true again within 2 s. `test_nothing_typed_on_disk`: after the run, `grep -r "quick brown" <tmp runtime/config/data dirs> <daemon stderr log>` finds nothing.
- [ ] **Step 5: Implement** `hotkeys_xlib.py`, `overlay_gtk.py`, `tray_gtk.py`, `daemon/main.py`.
- [ ] **Step 6: Run** `.venv/bin/pytest -m desktop -v` in the GNOME session — PASS. Then the manual pass: run `glanced`; in gedit, and in Chrome started with `google-chrome-stable --force-renderer-accessibility`, type `Teh quick brown fox jump over the lazy dog.`; confirm chip appears after the pause, never mid-typing; click a fix; press Ctrl+Z once and see the original; open an incognito window and a password field and see the grey Private chip.
- [ ] **Step 7: Commit** `feat: gtk overlay, hotkeys, tray, glanced and glance cli`. Update `README.md` with the three commands (`glanced`, `glance status`, `.venv/bin/pytest -m desktop`) in the same commit.
