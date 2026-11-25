# Glance Plan 0 — Assumption Spike Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove or disprove, on this machine, the assumptions the Glance design rests on (spec §9 step 1), and record the exact facts Plans 1–4 need, each with a go/no-go verdict.

**Architecture:** Throwaway probe scripts under `spike/` run with `/usr/bin/python3` (no venv, no packaging, no tests). The only durable output is `docs/superpowers/spike/findings.md` plus captured backend output shapes in `docs/superpowers/spike/shapes/`. Nothing in `spike/` is imported by product code; Plan 1 deletes the directory.

**Tech Stack:** system Python 3.10.12, PyGObject (Gtk 3, Atspi 2.0, Wnck), python-xlib (XTEST, XGrabKey), the installed `claude` 2.1.332 / `codex` 0.163.0 / `gemini` 0.55.1 / `opencode` 1.17.9 CLIs.

**Spec:** `docs/superpowers/specs/2026-11-25-glance-design.md`

**Plan sequence:** 0 spike (this) → 1 foundation → 2 backends/tone → 3 features → 4 install/hardening. A NO-GO here changes later plans; the fallback for each check says how.

## Global Constraints

- Use `/usr/bin/python3` only (pyenv python lacks `gi`). Install nothing with apt/pip. The single permitted download is the `harper-ls` release binary in Task 7, into `spike/bin/`.
- X11 session only. No xdotool/xclip, no evdev/uinput.
- Never send a message in any real app: unsent drafts only, cleared afterwards. Use the synthetic sentence `Teh quick brown fox jump over the lazy dog, i think its fine.` as test text everywhere; never real user text.
- AI calls: at most 3 calls per backend, each with the synthetic sentence, cheapest model. Task 6 is the only task that makes AI calls.
- Do not edit `~/.zshrc`, `.desktop` files, or `$EDITOR`. Flags and env vars are passed on the command line for the duration of a check.
- Probe scripts print lengths and structure, and the synthetic sentence only; they must not log text from the user's real windows. Run probes with only the app under test focused.
- Every check ends with one row appended to `docs/superpowers/spike/findings.md`: `| id | assumption | result (GO / NO-GO / GO-WITH-CHANGE) | evidence (numbers, exact strings) | consequence for plans |`.

## Review Focus

This plan produces no product code, so the list names the ways a spike gives false comfort; each is pinned to a check below.

1. Accessibility appears to work only because an app was already running with the flag from an earlier check — every app check starts from a fully quit app (`pgrep` shows no process) (Tasks 2, 3).
2. Caret extents are right on the primary monitor and wrong on the second (offset by 1920) or under fractional scroll — every extents check is done on both monitors and after scrolling the page (Task 2).
3. Paste write-back "works" but leaves the replacement in the clipboard-history extension — the history panel is inspected after each paste (Task 4).
4. An undo check passes in a `<textarea>` but the rich editor splits the replace into two undo steps — undo is checked in Slack and Discord composers, not just fixtures (Task 4).
5. A CLI's JSON shape is recorded from a success only — each backend is also recorded once signed-out-or-invalid-model so Plan 2 knows the failure shape (Task 6).

---

### Task 1: Repository, findings file, AT-SPI probe, GTK baseline

**Files:**
- Create: `.gitignore`, `docs/superpowers/spike/findings.md`, `spike/probe.py`

**Interfaces:**
- Produces: `spike/probe.py` — CLI `python3 spike/probe.py [--once]`. On every AT-SPI `object:state-changed:focused`, `object:text-changed`, `object:text-caret-moved` event for the focused object it prints one JSON line: `{"event", "app", "toolkit", "role", "states", "len", "caret", "caret_rect":[x,y,w,h], "prev_char_rect", "field_rect", "doc_url", "has_fffc", "editable_text_iface", "ms"}` where `ms` is the wall time of the AT-SPI calls for that event. All later tasks reuse it.
- Produces: `docs/superpowers/spike/findings.md` with the table header from Global Constraints and one section per task.

- [ ] **Step 1: Initialise the repo**

Run: `cd <repo> && git init -b main`. Write `.gitignore` containing `.venv/`, `__pycache__/`, `*.egg-info/`, `.pytest_cache/`, `.ruff_cache/`, `spike/bin/`, `spike/out/`. Commit the existing `docs/` tree: `git add .gitignore docs && git commit -m "docs: glance design spec and plans"`.

- [ ] **Step 2: Write `spike/probe.py`**

Use `gi.repository.Atspi`: `Atspi.EventListener.new(cb)`, register the three event names, run `GLib.MainLoop`. Caret rect via `Atspi.Text.get_character_extents(obj, caret, Atspi.CoordType.SCREEN)`; previous-char rect with `caret-1`; field rect via `Atspi.Component.get_extents`. `doc_url`: walk parents to the first `ROLE_DOCUMENT_WEB` and read `Atspi.Document.get_document_attribute_value(doc, "DocURL")`. `has_fffc`: `"￼" in text`. Call `Atspi.set_timeout(200, 200)` first.

- [ ] **Step 3: GTK baseline (check S1.1)**

Run `python3 spike/probe.py` and type the synthetic sentence in `gedit`. PASS when: role is `text`, `len` and `caret` track every keystroke, `caret_rect` has non-zero height and its x increases as you type, on both monitors.

- [ ] **Step 4: AT-SPI call cost (check S1.2)**

From the probe output, record p50/p95 of `ms`. GO when p95 ≤ 20 ms in gedit. Record the numbers; Plan 1's 200 ms timeout and idle-CPU target depend on them.

- [ ] **Step 5: Record and commit**

Append rows S1.1, S1.2 to findings. `git add spike/probe.py docs/superpowers/spike && git commit -m "spike: at-spi probe and gtk baseline"`.

**Go/no-go:** NO-GO on S1.1 means the machine's AT-SPI stack is broken — stop and report; nothing else in the design holds.

---

### Task 2: Chromium and Electron accessibility with the flag

**Files:**
- Create: `spike/fixture.html` (one `<textarea>`, one `<div contenteditable>`, one `<input type=password>`, tall filler so the page scrolls)
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Consumes: `spike/probe.py`.
- Produces: findings rows S2.1–S2.8, including the exact strings Plan 1's privacy gate and Plan 4's `.desktop` rewriter use.

For each of **Chrome** (`/usr/bin/google-chrome-stable`), **Brave** (`/usr/bin/brave-browser-nightly`), **Slack** (`/usr/bin/slack`), **Discord** (`/usr/bin/discord`): quit the app completely (`pgrep -f` empty), then launch with `--force-renderer-accessibility` appended on the command line.

- [ ] **Step 1: Text and caret (S2.1)** — in `spike/fixture.html` (browsers) and in an unsent draft to yourself (Slack: your own DM; Discord: a private test server channel), type the synthetic sentence. PASS per app when `len`/`caret` are exact after every keystroke, after a mouse click mid-text, after Ctrl+V of a word, and after Backspace.
- [ ] **Step 2: Caret extents (S2.2)** — PASS when `caret_rect` is within 4 px of the visible caret (compare against a screenshot with `gnome-screenshot -f spike/out/x.png`) on both monitors and after scrolling the fixture page by 300 px. Record which of `caret_rect` / `prev_char_rect` / `field_rect` is usable per app — this fixes the fallback order in Plan 1 `ui/placement.py`.
- [ ] **Step 3: Without the flag (S2.3)** — relaunch each app without the flag; record what the probe sees (expected: nothing). This is the string `glance doctor` reports.
- [ ] **Step 4: DocURL (S2.4)** — record the exact attribute name and value for the fixture page, `https://claude.ai/new`, `https://chatgpt.com/`, and `https://mail.google.com/` compose in Chrome, Brave and Firefox. Record whether Slack/Discord expose a URL or channel name (title string format, e.g. `#general - Workspace - Slack`).
- [ ] **Step 5: Rich entities (S2.5)** — in Slack and Discord drafts type a mention, a `:emoji:`, a link and an inline code span. Record how each appears in `Atspi.Text.get_text` (U+FFFC vs literal text) and whether `Atspi.Hypertext` reports links. Plan 1 `privacy/entities.py` patterns come from this row.
- [ ] **Step 6: Password and incognito (S2.6, S2.7)** — record role and states of the fixture's password input (expected `password text`). Open an incognito/private window in Chrome, Brave, Firefox; record the frame's accessible name and `_NET_WM_NAME` verbatim. GO when each has a stable title marker. NO-GO fallback (record which applies): detect by the toolbar's accessible "Incognito"/"Private" button, else treat every window of that browser launched with `--incognito` as private by reading `/proc/<pid>/cmdline`.
- [ ] **Step 7: CPU overhead (S2.8)** — with Chrome idle on the fixture page for 60 s, record `%CPU` of the browser process tree (`ps -o %cpu= --ppid …` summed, 3 samples) with and without the flag, and again while typing for 30 s. This is the "Chrome accessibility overhead measured and documented" number of spec §8.4.
- [ ] **Step 8: Commit** — `git commit -am "spike: chromium/electron accessibility findings"`.

**Go/no-go:** S2.1 or S2.2 failing for Chrome or Slack is NO-GO for Approach A in that app → that app is served by select-anything (Plan 3) only, and the acceptance list in Plan 4 drops it; record explicitly. Failing in *all four* → stop and report to the user.

---

### Task 3: Firefox snap accessibility

**Files:**
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Consumes: `spike/probe.py`, `spike/fixture.html`.
- Produces: findings rows S3.1–S3.2.

- [ ] **Step 1: (S3.1)** Quit Firefox (snap, 161.0). Launch `firefox spike/fixture.html`, repeat Task 2 Steps 1, 2, 4, 6 for the textarea and contenteditable.
- [ ] **Step 2: (S3.2)** If nothing is exposed, relaunch with `GNOME_ACCESSIBILITY=1 firefox …` and then with `accessibility.force_disabled=0` checked in `about:config`; record which was needed. Also record whether the snap can reach the AT-SPI bus at all (`busctl --user --address="$(busctl --user call org.a11y.Bus /org/a11y/bus org.a11y.Bus GetAddress | cut -d'"' -f2)" list | grep -c :`).
- [ ] **Step 3: Commit** findings.

**Go/no-go:** NO-GO → Firefox is select-anything-only in v1 and `glance doctor` says so; no further consequence.

---

### Task 4: Write-back mechanics

**Files:**
- Create: `spike/writeback.py`
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Consumes: `spike/probe.py` helpers (import as a module).
- Produces: `spike/writeback.py` — CLI `python3 spike/writeback.py --start N --end M --text S --select {atspi,keys} --insert {type,paste}`; after a 3 s countdown it acts on the focused field, re-reads after 150 ms, and prints `{"before_len","after_len","matches_expected","select_ms","insert_ms"}`. Findings rows S4.1–S4.7.

- [ ] **Step 1: Write `spike/writeback.py`** — selection by `Atspi.Text.set_selection(obj, 0, start, end)` (add with `add_selection` when there is none) or by `set_caret_offset(end)` + XTEST Shift+Left × (end−start). Typing via `Xlib.ext.xtest.fake_input` with keysym→keycode lookup. Paste: own CLIPBOARD via `Gtk.Clipboard`, advertising targets `UTF8_STRING`, `text/plain;charset=utf-8` and `x-kde-passwordManagerHint` (value `secret`), send Ctrl+V, then restore the previous clipboard text.
- [ ] **Step 2: Selection (S4.1)** — replace `Teh`→`The` in: gedit, fixture textarea, fixture contenteditable, Slack draft, Discord draft. Record per app which `--select` mode works. GO when at least one mode works in each.
- [ ] **Step 3: Typing vs paste (S4.2, S4.3)** — in each app replace `jump`→`jumps` with `--insert type`, then replace the whole sentence (≥ 60 chars) with `--insert paste`. Record `matches_expected`, timings, and the minimum inter-key delay at which typing 80 chars drops no characters (try 0, 2, 5 ms).
- [ ] **Step 4: Undo (S4.4)** — after each replace press Ctrl+Z once by hand. PASS when the field text equals the original. Record apps where one replace = 2 undo steps (select+insert split) and how many Ctrl+Z are needed.
- [ ] **Step 5: Clipboard hygiene (S4.5, S4.6)** — copy `CLIP-ORIGINAL` first; after a paste replace, PASS when `Gtk.Clipboard.wait_for_text()` is `CLIP-ORIGINAL` and the `gnome-clipboard@b00f.github.io` history panel does not list the replacement. Repeat with an image in the clipboard (copy from a screenshot) and record whether it survives.
- [ ] **Step 6: AT-SPI EditableText is unsafe (S4.7)** — in the Slack draft only, call `Atspi.EditableText.insert_text`; record whether the editor's model desyncs (text reappears/disappears on next keypress). This documents why the spec forbids it; do not use it elsewhere.
- [ ] **Step 7: Clear all drafts; commit** findings and `spike/writeback.py`.

**Go/no-go:** paste failing S4.5 (history captured) → Plan 1 raises the XTEST typing limit from 80 to 400 chars and marks longer replaces "Copy rewrite" in Slack/Discord. Undo needing N>1 steps → Plan 1 `[apps.<wm_class>] undo_steps = N` gets that default for the app.

---

### Task 5: Overlay window and global keys

**Files:**
- Create: `spike/overlay.py`
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Produces: findings rows S5.1–S5.4.

- [ ] **Step 1: Write `spike/overlay.py`** — a 22 px `Gtk.Window(type=Gtk.WindowType.POPUP)` with `set_accept_focus(False)`, `set_keep_above(True)`, RGBA visual, that follows `caret_rect` from the probe (below-right, +4 px). A second fully transparent window with an empty input shape (`input_shape_combine_region(cairo.Region())`) draws grey text after the caret. Grab Super+Alt+Space with `XGrabKey` on the root window for the 4 lock-mask combinations (none, Lock, Mod2, Lock|Mod2); grab plain Tab for 3 s after each Super+Alt+Space, then ungrab.
- [ ] **Step 2: (S5.1)** PASS when the chip never takes keyboard focus (keep typing in gedit, Chrome, Slack while it moves) and stays on the monitor that holds the caret.
- [ ] **Step 3: (S5.2)** PASS when the ghost window is click-through (click the text underneath it) and its text baseline aligns within 3 px using font size = `0.75 × caret_rect.h` px; record the factor that fits best in Chrome, Slack, gedit.
- [ ] **Step 4: (S5.3)** PASS when Super+Alt+Space fires in every app with NumLock on and off, and GNOME does not already bind it (`gsettings list-recursively org.gnome.desktop.wm.keybindings org.gnome.shell.keybindings org.gnome.mutter.keybindings | grep -i "<Super><Alt>"`). Do the same for Super+Alt+F, Z, P, Return, 1–4.
- [ ] **Step 5: (S5.4)** PASS when Tab moves focus/indents normally before and after the 3 s grab window, and the grabbed Tab is not delivered to the app during it.
- [ ] **Step 6: Commit.**

**Go/no-go:** a taken hotkey → record the replacement chord; Plan 1 defaults in `config.py` change to match. Focus stealing that cannot be fixed with a POPUP window → stop and report.

---

### Task 6: Backend CLI shapes

**Files:**
- Create: `spike/backends.sh`, `docs/superpowers/spike/shapes/{claude,codex,gemini,opencode}-{analyze,rewrite,error}.txt`, `spike/schema.json`
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Produces: the shape files (raw stdout, one file per call) that Plan 2 copies into `tests/fixtures/backends/`; findings rows S6.1–S6.6. `spike/schema.json` is `{"type":"object","properties":{"tone":{"type":"string"},"issues":{"type":"array","items":{"type":"object","properties":{"quote":{"type":"string"},"occurrence":{"type":"integer"},"message":{"type":"string"}},"required":["quote","occurrence","message"]}}},"required":["tone","issues"]}`.

All commands run from an empty directory `spike/out/empty/` with stdin = the synthetic sentence. Time each with `/usr/bin/time -f %e`.

- [ ] **Step 1: claude (S6.1)** — analyze: `claude -p --model haiku --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence --system-prompt "Return the tone and issues of the user's text." --json-schema "$(cat ../../schema.json)" --output-format json`. Rewrite: same without `--json-schema`, with `--output-format stream-json --include-partial-messages --verbose`. Record: where the structured object sits in the JSON, the event `type`s that carry text deltas, cold/warm seconds.
- [ ] **Step 2: claude warm spawn (S6.2)** — start the rewrite command with `--input-format stream-json` and an open stdin pipe; wait 60 s; then write one `{"type":"user","message":{"role":"user","content":"<sentence>"}}` line and close stdin. GO when the first delta arrives ≥ 1 s sooner than cold and the idle process does not exit. Record idle RSS and whether `--json-schema` also works in this mode.
- [ ] **Step 3: codex (S6.3)** — `codex exec --json --ephemeral --skip-git-repo-check --ignore-rules -s read-only --output-schema ../../schema.json -C . -`. Record JSONL event types, where the final message is, whether deltas stream, whether adding `--ignore-user-config` still authenticates, and the model name the CLI reports as default.
- [ ] **Step 4: gemini (S6.4)** — `gemini -p "Return JSON {tone, issues[]} for the text on stdin." -o json --approval-mode plan --skip-trust`, then `-o stream-json` for the rewrite. Record the JSON envelope keys, whether the answer is fenced in ```` ```json ````, whether a session file is written (`gemini --list-sessions` before/after) and the working `--delete-session` invocation, and how to disable extensions/MCP (`-e` with an empty list or `--allowed-mcp-server-names none`).
- [ ] **Step 5: opencode (S6.5)** — `opencode serve --port 0 --hostname 127.0.0.1 --pure` in the background; record the line that prints the URL. Then `opencode run --attach <url> --format json --pure "…"`; record the event types, the key holding the session id, whether the message can be given on stdin instead of argv (typed text must not appear in `/proc/*/cmdline`; if not, record the server's HTTP route for posting a message from `curl -s <url>/doc`), and that `opencode session delete <id>` removes it (`opencode session list` before/after). Record the default `provider/model`.
- [ ] **Step 6: Health and failure shapes (S6.6)** — record stdout and exit code of `claude auth status --json`, `codex login status`, `opencode auth list`, and for gemini the presence of `~/.gemini/oauth_creds.json` / `GEMINI_API_KEY`. For each backend make one call with `-m`/`--model` set to `glance-nonexistent-model` and save stdout+stderr+exit code to `*-error.txt`.
- [ ] **Step 7: Scrub and commit** — confirm the shape files contain no account email, token or session path (`grep -iE "@|token|/home/" docs/superpowers/spike/shapes/*`; replace hits with `<redacted>`). Commit.

**Go/no-go:** S6.2 NO-GO → Plan 2's `ClaudeBackend` cold-spawns per request and the CLI latency target of spec §8.3 is recorded as at-risk. Any backend whose structured output cannot be obtained in ≤ 8 s → it ships in Plan 2 as "rewrite only" and the onboarding picker says so.

---

### Task 7: Ctrl+G, Harper, and the verdict

**Files:**
- Create: `spike/fake-editor.sh`, `spike/harper_probe.py`
- Modify: `docs/superpowers/spike/findings.md`

**Interfaces:**
- Produces: findings rows S7.1–S7.5 and a final `## Verdict` section.

- [ ] **Step 1: Ctrl+G (S7.1, S7.2)** — `spike/fake-editor.sh` appends to `spike/out/editor.log`: `"$@"`, the byte length of the file argument, `env | grep -iE "^(CLAUDE|EDITOR|VISUAL|TERM)"`, and `ps -o pid=,comm= -p $PPID` walked up 4 levels; then appends ` EDITED` to the file and exits 0. Run `EDITOR="$PWD/spike/fake-editor.sh" claude` in Ghostty, type the synthetic sentence, press Ctrl+G. PASS when the prompt comes back with ` EDITED`. Record: argv, file path pattern and extension, every `CLAUDE*` env var name, the parent `comm` chain, and what happens on exit code 1 (prompt unchanged?). Do not submit the prompt.
- [ ] **Step 2: Invoker detection (S7.2)** — from the log choose the rule Plan 3's `glance-edit` uses to decide "called by Claude Code": prefer an env var set by Claude Code (record its name); fallback an ancestor process named `claude`. Also run `EDITOR=spike/fake-editor.sh git commit --allow-empty` in a scratch repo and confirm the rule says "not Claude Code".
- [ ] **Step 3: Harper (S7.3–S7.5)** — download the latest `harper-ls` x86_64-unknown-linux-gnu release asset from `https://github.com/Automattic/harper/releases` into `spike/bin/`; record the version and SHA-256. `spike/harper_probe.py` speaks LSP over stdio: `initialize`, `initialized`, `textDocument/didOpen` (languageId `plaintext`, the synthetic sentence), reads `textDocument/publishDiagnostics`, then `textDocument/codeAction` for each diagnostic. Record: diagnostic `code`/`source`/`range` units (UTF-16?), the code-action title/edit shape, ms from `didChange` to diagnostics over 20 edits (GO ≤ 50 ms p95), and the settings key that sets the user dictionary path and disables a rule (`workspace/didChangeConfiguration`, expected under `"harper-ls"`: `userDictPath`, `linters`).
- [ ] **Step 4: Write the verdict** — a `## Verdict` section listing every NO-GO / GO-WITH-CHANGE row and the exact edit it requires in Plans 1–4 (file, task, value). If all GO, say so.
- [ ] **Step 5: Commit** — `git add docs/superpowers/spike spike/*.py spike/*.sh spike/*.html spike/*.json && git commit -m "spike: ctrl+g, harper, verdict"`.

**Go/no-go:** Ctrl+G not invoking `$EDITOR` in Claude Code 2.1.x → Plan 3 Tasks 7–8 are replaced by select-anything in the terminal and the user is told. Harper > 50 ms p95 → Plan 1 runs Harper on the debounce only (never per keystroke), no other change.
