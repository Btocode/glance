# Glance — system-wide writing assistant for Linux (design)

Date: 2026-11-25 · Status: draft for user approval

## 1. Intent

The user writes all day on Linux — prompts to AI tools (Claude Code, claude.ai, ChatGPT), messages to teammates (Slack, Discord), browser forms, terminal. They lose time going back and forth fixing grammar, punctuation, clarity and tone, where the right tone depends on who will read it. Glance is an always-on writing layer that follows what they type in any app and offers fixes and audience-appropriate tone rewrites right where they are typing, so they stop doing that cleanup by hand.

The user explicitly wants "the hard problem, not something basic": live, in-place, across apps — not a clipboard hotkey toy. They granted the designer freedom to add features beyond what they listed.

## 2. Goals, non-goals, anti-goals

**Goals**
- Live grammar/spelling/punctuation fixes in the focused text field of any supported app.
- Live tone detection matched to the audience (app, channel, URL, content), with a floating chip next to the caret offering tone rewrites that apply in place.
- Works in Chrome/Brave/Firefox, Slack, Discord, GTK apps, AI chat web apps, and Claude Code (via Ctrl+G).
- User picks the AI backend: claude, codex, gemini, opencode (CLIs already installed), or a direct API key.
- Safe enough to leave running all day.

**Non-goals (v1)**
- Wayland support. Google Docs (canvas). Local models / Ollama (user: not now). Squiggle underlines. Auto-applying changes without a click.

**Anti-goals — failure even if it "works"**
- Corrupting the text in someone's editor (silent desync, lost text).
- Adding any perceptible typing latency, or popping UI while keys are going down.
- Sending passwords, secrets, password-manager / remote-desktop / incognito content, or shell commands to any AI.
- Nagging: false positives with no way to silence them.
- Writing anything the user typed to disk.

## 3. Environment and constraints (verified on the machine)

- Ubuntu 22.04.5, GNOME 42.9, **X11**, dual 1080p monitors, 12 cores, 30 GB RAM, GTX 1650 4 GB.
- System `/usr/bin/python3` 3.10.12 with PyGObject 3.42.1; imports OK: Gtk 3, Gdk, Atspi 2.0, pyatspi, Wnck, IBus, Secret, Notify, python-xlib, dbus, cairo. (pyenv python lacks `gi` — must use system python.)
- X extensions: XTEST, RECORD, XInput2, SHAPE, XFIXES, Composite. No xdotool/xclip needed. `/dev/uinput` is root-only → avoid evdev/uinput.
- GNOME `toolkit-accessibility` = true.
- Chrome, Slack, Discord currently expose almost nothing on the AT-SPI bus → need `--force-renderer-accessibility`.
- Terminal in use: **Ghostty 1.4 (GTK4)** — exposes no text via AT-SPI. Process chain ghostty → zsh → claude, no tmux.
- A clipboard-history GNOME extension is enabled (matters for paste write-back).
- Must be denylisted: AnyDesk, Remmina, Seahorse (running now), password managers.
- CLIs installed: `claude` 2.1.332, `codex` 0.163.0, `opencode` 1.17.9, `gemini` (via nvm node 24). Measured `claude -p --model haiku` with stripped config: 5.2 s cold, 3.1 s warm (1.6 s API, ~1.4 s process startup, ~250 MB RSS).

## 4. Approaches considered

- **A. AT-SPI accessibility for reading the focused field + caret, editor-native input for writing back, keystroke reconstruction only for allowlisted terminal programs. — CHOSEN.** Exact text and caret position in browsers/Electron/GTK; overlay can be anchored to the caret. Cost: Chromium/Electron apps need a launch flag; Chrome may pay some CPU.
- **B. Keystrokes everywhere (XRecord/XInput2).** One mechanism for all apps, but blind to paste, clicks, autocomplete and cursor moves → frequently wrong text; can't position the chip. Rejected as primary; used only inside allowlisted terminal programs.
- **C. Per-app plugins (browser extension, shell plugin, …).** Most accurate per app, but many projects and no real path into Slack/Discord desktop. Rejected except the tiny Claude Code `$EDITOR` shim.
- Custom IBus input engine — rejected for v1: a bug there means the user cannot type at all.

User approved A (implicitly via the Fable design) and confirmed the Chrome/Slack/Discord accessibility flag.

## 5. Decisions made by the user

1. **Accessibility flag:** yes — Glance writes `.desktop` overrides adding `--force-renderer-accessibility` for Chrome, Brave, Slack, Discord (and other detected Chromium/Electron apps); reversible via uninstall/`glance doctor --undo`.
2. **Backend:** the user chooses. Supported: **claude, codex, gemini, opencode**, plus optional direct API (Anthropic / OpenAI-compatible). Glance detects which CLIs are installed and signed in. **If none is connected, Glance must clearly tell the user to connect one** (onboarding screen + tray state + `glance doctor`), with the exact command per backend (e.g. `claude` → log in; `codex login`; `gemini` → auth; `opencode auth login`). Local grammar keeps working without a backend.
3. **Claude Code:** Ctrl+G flow (Glance's `glance-edit` as `$EDITOR`) — exact, user-triggered. No live keystroke reconstruction in v1 (see calls below).
4. **Assertiveness:** quiet chip that only speaks for real errors or a tone mismatch, **plus send-guard** (non-blocking warning before sending a risky chat message).
5. Ollama/local models: not now.
6. **Audience of the software:** this PC first (Ubuntu 22.04, GNOME 42, X11, system Python), with clean, configurable code so it can be packaged for others later. No distro/packaging matrix in v1.
7. **AI usage: frugal by default.** `analyze` after a pause; tone rewrites run only on hover/click (2–3 s wait on CLI backends). Speculative pre-generation is on by default only for the `api` backend; a config toggle enables it for CLI backends. Configurable daily cap (default 300 calls/day, 10/min); when reached, chip shows "AI paused (limit)" and local grammar continues.
8. **Language:** mostly English, sometimes Banglish (Bangla in Latin letters). Glance detects non-English/Banglish spans per sentence and suppresses local grammar/spelling flags there (no nagging); LLM tone analysis still runs and is told the text may mix English and Banglish, and rewrites preserve the user's language choice.

### Calls made by Claude (from the builder review — user may override)

- **Terminal live reconstruction is out of v1.** Terminals get Ctrl+G (Claude Code) and select-anything only. The XInput2 raw-key path, "unverified" chip state and terminal bracketed-paste write-back move to Later. Capture in v1 is AT-SPI only.
- **Ctrl+G UX and `$EDITOR` scope:** `$EDITOR` is not changed globally. The installer adds a `claude` shell wrapper (zsh function) that sets `EDITOR=glance-edit` only for Claude Code. `glance-edit` opens the Glance panel as a normal focused window over the terminal showing the prompt with issues, tone chips and Prompt Sharpener; Apply writes the file and exits; "Open in my editor" chains to the user's real `$EDITOR`; Esc exits unchanged. If `glance-edit` is invoked by anything other than Claude Code it passes straight through to the real editor.
- **Rich content (mentions, emoji, links, code blocks, formatting):** grammar fixes use minimal-span edits that never touch protected entities. Tone rewrites of a field containing protected entities keep them as placeholders through the LLM; if write-back can't restore them exactly, Glance does not apply and offers "Copy rewrite" instead. Flattening rich content is never acceptable.
- **"Nothing typed on disk" scope:** applies to Glance's own storage, logs and cache. The personal dictionary and per-app/channel/URL overrides are exempt (user-chosen words and rules, not message text). Third-party backends: Glance uses each CLI's no-persistence/ephemeral options and deletes opencode sessions after each call; residual storage by a backend is documented in the backend picker. Clipboard-history skip is best-effort via the password-manager hint; short fixes use XTEST typing to avoid the clipboard.
- **Acceptance testing:** automated matrices run against local fixture pages that embed the real editor libraries (contenteditable, ProseMirror/Slate/Lexical, Quill, textarea) in Chrome/Brave/Firefox, plus a GTK test app. Real Slack, Discord, Gmail, claude.ai and ChatGPT get a scripted manual smoke checklist in the user's real profiles, unsent drafts only, never sending. The numbers in §8 are hard gates on fixtures and targets on live apps.

## 6. Architecture

**Runtime:** Python 3.10 system interpreter, in a `uv` venv with `--system-site-packages`; GTK 3 (GTK 4 removed absolute window positioning). Grammar via **Harper** (`harper-ls`, Rust, LSP over stdio, ~10 ms/check). New deps: harper-ls binary, httpx, ayatana appindicator GIR for tray.

**Processes** (newline-delimited JSON over `$XDG_RUNTIME_DIR/glance/*.sock`, mode 0600):
- `glance-capture` — AT-SPI listeners (focus, text-changed, caret-moved), Wnck active-window tracking (terminal keystroke reconstruction is Later, not v1). Emits `FieldSnapshot{app, wm_class, title, url, role, text, caret, caret_rect, field_id, capture_confidence}`. Also executes write-back. All AT-SPI calls have 200 ms timeouts; process is isolated so a hung app can't freeze the UI; supervised restart.
- `glanced` — privacy gate, audience resolver, check scheduler, cache, backend supervisor, overlay UI, tray.
- `harper-ls` child.
- Backend workers (pre-spawned CLI processes or HTTP client).
- `glance` CLI: `status | pause [30m] | resume | doctor [--fix|--undo] | backend [list|use X|test] | rewrite --tone X` (stdin→stdout) `| profile`.
- `glance-edit` — `$EDITOR` shim for Claude Code Ctrl+G.
- systemd user unit bound to `graphical-session.target`, `Restart=on-failure`.

**Data flow:** snapshot → privacy gate → Harper immediately (chip count) → after 1.2 s idle, ≥25 chars and meaningful change → LLM `analyze` → chip shows tone/issues → rewrite on hover/click (speculative pre-generation of the top two tones after 3 s idle only when enabled — default on for `api`, off for CLI backends) → click/hotkey applies → verify. A generation counter cancels stale requests (SIGTERM process group / close stream).

**Write-back (never AT-SPI EditableText in Electron/ProseMirror editors):** verify text hash unchanged → select range (`Text.set_selection`, fallback synthesized Shift+arrows) → replace via XTEST typing (≤ ~80 chars) or clipboard swap + Ctrl+V (Ctrl+Shift+V bracketed paste in terminals), restoring the clipboard and marking it with the password-manager hint so clipboard history skips it → re-read after ~150 ms; on mismatch send Ctrl+Z and offer "Copy instead". One apply = one native undo step. Glance also keeps "Revert last apply" (Super+Alt+Z).

**Overlay:** override-redirect GTK3 window, never takes focus, clamped to monitor. Position fallback: caret extents → previous character extents → field bottom-right → window bottom-right.
- Chip (22 px, below-right of caret) appears only after ≥15 chars and a pause, never while typing. States: hidden · idle dot · `N` local fixes · spinner · tone badge ("Blunt · 3") · grey paused/private · "AI paused (limit)" · red "no AI connected".
- Panel (click chip or Super+Alt+Space): detected tone + 3–4 audience-appropriate tone chips (e.g. Friendlier / More direct / Shorter / Professional; in AI contexts: Sharpen prompt / Add structure / Shorter) + free-text "Rewrite as…"; hover streams a diff preview; click/Enter applies. Below: issue list, accept per item, "Fix all".
- Global keys (XGrabKey): Super+Alt+Space panel · Super+Alt+F fix all safe grammar · Super+Alt+1–4 apply tone chip N · Super+Alt+Z revert · Super+Alt+P pause · Esc dismiss/snooze for field.

**Audience & tone:** deterministic resolver first (ordered rules over wm_class, title regex, URL from AT-SPI DocURL, terminal foreground process) → profiles `ai_prompt`, `team_channel`, `dm`, `email`, `public_post`, `code_review`, `default`, each defining target tone, offered chips, strictness (e.g. don't flag lowercase in DMs), and whether to check. LLM `analyze` gets the profile and returns observed tone, mismatch flag + reason, and issues anchored by quoted substring + occurrence index (never char offsets). User overrides remembered per app/channel/URL.

**Backends:** one interface `analyze(text, ctx) -> Analysis`, `rewrite(text, tone, ctx) -> stream[str]`, `cancel()`, `health()` (installed? signed in?).
- claude: run in empty dir, `claude -p --model haiku --tools "" --strict-mcp-config --setting-sources "" --no-session-persistence --system-prompt … --json-schema … --output-format json`; rewrites with `stream-json --include-partial-messages`; one pre-spawned warm process per request. Flags are mandatory (user's config loads many plugins/MCP; `--tools ""` prevents typed text triggering tools).
- codex: `codex exec --json --ephemeral --skip-git-repo-check -s read-only --output-schema … -m <model> -`.
- gemini: non-interactive prompt mode with JSON output; exact flags to be confirmed against `gemini --help` in the spike.
- opencode: private `opencode serve` on loopback, `opencode run --attach <url> --format json -m provider/model`; validate + one repair retry; delete session after.
- api (optional): Anthropic / OpenAI-compatible streaming via httpx; key in libsecret; the sub-second lane.
- Common: 8 s analyze / 20 s rewrite timeouts; circuit breaker after 3 failures; fallback only to other backends the user enabled; concurrency 1 + 1 speculative; per-minute and daily call budgets; on battery → on-demand only.
- Backend selection: onboarding picker on first run (shows detected + signed-in status), changeable in tray and `glance backend use X`. If none is connected: tray icon and chip show "Connect an AI", clicking opens a setup window with per-backend instructions and a "Test" button.

**Privacy gate (before anything leaves capture):** skip password-role fields; app denylist (password managers, Seahorse, AnyDesk, Remmina, VM/RDP); incognito/private windows (title match); URL denylist (banking, login/SSO); terminals allowlist-only, never a bare shell prompt; secret redaction with placeholders restored after rewrite (API keys, JWTs, PEM, Luhn-valid cards, high-entropy tokens); always-visible pause hotkey and tray state. Cache is in-memory LRU only; logs contain lengths/hashes only.

**Config:** `~/.config/glance/config.toml`, hot-reloaded: `[general]` hotkeys/idle/min_chars, `[backend]` choice/models/budgets, `[privacy]`, `[[profile]]`, `[apps.<wm_class>]` capture/write-back overrides, `[ui]`. Personal dictionary + ignored rules in `~/.local/share/glance/`.

## 7. v1 feature set

- Live local grammar/spelling/punctuation (Harper, offline).
- LLM clarity + tone analysis with audience mismatch detection.
- Tone chips with streamed diff preview, in-place apply, verify, revert; free-text "Rewrite as…".
- **Autocomplete** (user request, added after review). Grey "ghost text" continuation drawn in a transparent click-through overlay right after the caret (font size approximated from caret height). Tab accepts all, Ctrl+→ accepts one word, and typing anything else dismisses it. Tab/Ctrl+→ are grabbed via XGrabKey **only while a suggestion is visible** and released immediately after, so Tab works normally the rest of the time. Two modes:
  - *On-demand* (Super+Alt+Enter): available with every backend.
  - *Live* (after ~600 ms pause, mid-sentence or end of line): on by default only with the `api` backend, because CLI backends take about 3 s (too slow to feel like autocomplete) and would burn plan quota. Can be enabled for CLI backends in config.
  
  Context sent: the field text up to the caret (≤2k chars) plus the audience profile. Completions are short (≤1 sentence), match the user's language (English/Banglish) and tone, and are cancelled the moment a key is pressed. Live calls count toward the daily cap. Off in password/denylisted contexts like everything else. (A small local model via Ollama would make live autocomplete free and instant; Later.)
- **Prompt Sharpener** in AI contexts (goal / context / constraints / output format).
- **Send-guard** (non-blocking amber chip before Enter in chat apps for hostile tone, referenced-but-missing attachment/link, etc.).
- **Select-anything rewrite** via PRIMARY selection + hotkey — universal fallback for apps without capture.
- Personal dictionary, "don't flag this again" per rule/app.
- Audience profiles with remembered overrides.
- Backend choice (claude / codex / gemini / opencode / api) with "connect an AI" onboarding.
- Privacy gate, pause, `glance doctor`, Ctrl+G shim for Claude Code, autostart, install/uninstall script.

**Later:** live keystroke reconstruction in terminal TUIs (codex/opencode/gemini), squiggle underlines, style memory (learn the user's voice), reply drafting from visible thread (separate consent), snippets, translate in place, local stats digest, Ollama backend, Google Docs extension, Wayland, warm codex app-server.

## 8. Acceptance criteria ("production ready")

1. Capture: correct text + caret in ≥99% of 200 scripted edits per app for Chrome, Brave, Firefox, Slack, Discord, claude.ai & ChatGPT composers, Gmail, a GTK app, Claude Code via Ctrl+G.
2. Write-back: zero silent corruption across 500 scripted applies per app (verified or auto-reverted with notice); one Ctrl+Z restores; clipboard restored.
3. Latency: local fixes ≤150 ms after pause; analyze p50 ≤3.5 s CLI / ≤1.2 s API; pre-generated tone apply ≤200 ms (when speculation is on); on-demand rewrite first visible token ≤3.5 s CLI / ≤1 s API; chip reposition ≤50 ms; live autocomplete ghost text ≤1.2 s p50 after pause on `api`; Tab behaves normally whenever no suggestion is visible (zero stolen keystrokes in tests); no measurable added keystroke latency.
4. Resources: idle CPU <0.5%; <3% of one core while typing; <250 MB RSS excluding backend workers; Chrome accessibility overhead measured and documented.
5. Privacy: automated tests prove nothing leaves from password fields, denylisted apps, incognito, or shell prompts; secret redaction recall ≥99% on a corpus; log audit proves no typed text on disk.
6. Resilience: survives killed children, hung target app, backend logout/rate-limit, suspend/resume, monitor hotplug; restart ≤2 s; degraded mode keeps local grammar.
7. No backend connected → clear "Connect an AI" state everywhere; local grammar still works.
8. Install: one script, autostart, `glance doctor` green; uninstall restores `.desktop` overrides and `$EDITOR`.

## 9. Build order

1. Spike (~2 days, throwaway): prove Electron accessibility + caret extents with the flag, paste write-back in Slack/Discord, Ctrl+G in Claude Code 2.1.x, Firefox snap accessibility, gemini/codex/opencode JSON shapes.
2. Capture + overlay. 3. Harper. 4. Backend layer + claude + tone chips. 5. Privacy gate + doctor + onboarding. 6. codex/gemini/opencode/api backends. 7. Autocomplete, Send-guard, Prompt Sharpener, select-anything. 8. Hardening against the acceptance matrices.

## 10. Left to the builder

Exact module layout, test framework, prompt wording and JSON schemas, default model per backend, exact debounce constants (within the latency criteria), tray menu layout, visual styling of chip/panel, rule ordering for audience profiles.
