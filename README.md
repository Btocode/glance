# Glance

System-wide writing assistant for Linux/X11. Offline grammar fixes in place, no AI required.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/Btocode/glance/main/install.sh | bash
```

Needs an X11 session ("Ubuntu on Xorg") on x86_64, a running systemd user manager, and the
system Python (`/usr/bin/python3`, 3.10 to 3.13) with GTK 3, AT-SPI and Wnck. If a system
package is missing the installer prints the exact `sudo apt install …` line and stops; it never
runs `sudo` itself unless you pass `--with-apt`:

```sh
curl -fsSL https://raw.githubusercontent.com/Btocode/glance/main/install.sh | bash -s -- --with-apt
```

`--with-apt` runs one `sudo apt-get install -y` with a fixed list: the missing packages it just
listed plus the tray icon library. It echoes the command first and does not run `apt-get
update`. On a system without `apt-get` it stops and names the packages to install another way.

What it does: clones Glance into `~/.local/share/glance-src` (a re-run updates it with
`git pull --ff-only`), builds a venv there, downloads the grammar engine `harper-ls` (SHA-256
pinned), links `glance`, `glanced`, `glance-capture` and `glance-edit` into `~/.local/bin`,
installs and starts the systemd user unit `glance.service`, then runs `glance doctor --fix` and
prints the result. When the `claude` CLI is installed and your login shell is zsh, it asks
before adding the `claude` Ctrl+G wrapper to `.zshrc` (no terminal means no); it never changes
`$EDITOR`. If `glance doctor --fix` crashes the installer stops with its error; if some checks
are only still red it finishes and tells you to run `glance doctor`. Existing files in `~/.local/bin` that are not Glance's
are never overwritten. Running it again is safe.

Options: `--with-apt`, `--yes` (accept the wrapper), `--no-wrapper`, `--no-desktop-overrides`
(leave the Chrome/Brave/Slack/Discord launchers alone), `--no-start`, and `--from-local` to
install from a checkout (`./install.sh --from-local`).

Afterwards: run `glance doctor`, and connect an AI if you want rewrites and tone help — the
`claude`, `codex`, `gemini` or `opencode` CLI, or an API key. Grammar fixes work without one.

## Doctor

`glance doctor` checks the session, system packages, grammar engine, daemon, hotkeys, app
launcher flags, AI backend, Ctrl+G wrapper and autostart, and says what to do about each
problem. `glance doctor --fix` repairs what Glance owns (`--skip ID` leaves one check alone,
`--app ID` opts another Chromium/Electron app in); `glance doctor --undo` takes those changes
out again.

## Uninstall

```sh
~/.local/share/glance-src/uninstall.sh          # or ./uninstall.sh in your checkout
```

Stops and removes the unit, undoes what `glance doctor --fix` set up for launchers and the
`claude` block in `.zshrc` (byte for byte), removes Glance's symlinks, its data in
`~/.local/share/glance` and the source it cloned. `--keep-data` keeps your personal dictionary
and ignored rules. `~/.config/glance/config.toml` is always kept. Uninstall leaves GNOME's
toolkit-accessibility setting on (`glance doctor --fix` turned it on); turn it off yourself
with `gsettings set org.gnome.desktop.interface toolkit-accessibility false`. It is safe to run twice. If
the undo cannot run (Glance's environment is broken, even through the system Python), it keeps
`~/.local/share/glance/desktop-overrides.json`, the install record and the source, lists the
launcher files and `.zshrc` block still in place, and exits non-zero: re-run the installer and
then the uninstaller, or remove the listed files by hand.

## Development

`uv venv --python /usr/bin/python3 --system-site-packages .venv && uv pip install -e ".[dev]"`,
then `.venv/bin/pytest`.

## Running

- `glanced` starts the daemon (it starts `glance-capture` and `harper-ls` itself and restarts
  them if they die). Fetch the grammar checker once with `scripts/fetch-harper.sh`.
- `glance status` shows what it is doing (never any text); `glance pause [30m|2h|45s]` and
  `glance resume` switch checking off and on. They print `glanced is not running` and exit 3
  when the daemon is down.
- `.venv/bin/pytest -m desktop` runs the tests that need the live GNOME session. Stop your
  own `glanced` first and keep your hands off the keyboard: they press hotkeys and type into
  their own test window.
- `.venv/bin/pytest -m xvfb` runs the slower end-to-end tests that bring up their own private
  Xvfb server and D-Bus session (never your display); the default run leaves them out.
- Tests marked `paid` make real, billed AI calls. They are skipped unless `GLANCE_PAID=1` is
  set, whatever `-m` expression you pass: `GLANCE_PAID=1 .venv/bin/pytest -m paid`.

When a sentence you are typing has fixes, a quiet strip under the field shows it corrected.
Alt+Enter (or its "Use" button) puts the corrected sentence in, as one Ctrl+Z step; plain
Enter is never taken, so it still sends in chat apps. "⋯" (or Super+Alt+Space) opens the
tone rewrites, "Rewrite as…" and the list of fixes. Alt+Enter is only grabbed while the strip
shows a fix; set another key with `use_key` under `[ui]` in the config.

Hotkeys: Super+Alt+Space panel, Super+Alt+F fix all safe grammar, Super+Alt+Z revert the last
change, Super+Alt+P pause, Esc closes the panel. Change them under `[general.hotkeys]` in
`~/.config/glance/config.toml`. A shortcut that another program or GNOME itself already uses
is left alone and reported in a notification and in `glance status`.

For the tray icon install `sudo apt install gir1.2-ayatanaappindicator3-0.1`; without it
Glance runs the same, with no icon.

## Connect an AI

Grammar checks run on your computer. Tone hints and rewrites need one AI that **you** choose;
until one is connected the chip, the tray and `glance doctor` all say "Connect an AI" and the
grammar checks keep working. Sign in with the vendor's own tool, then choose it:

| AI | Connect |
| --- | --- |
| Claude | run `claude` and log in |
| Codex (experimental) | `codex login` |
| Gemini (experimental) | run `gemini` to authenticate (or use an API key) |
| OpenCode (experimental) | `opencode auth login` |
| API key | `glance backend set-key` (stored in the system keyring only) |

- `glance backend list` shows what is connected and the command for the rest; `glance backend
  use NAME` chooses one (exit 2, with its sign-in command, when it is not connected);
  `glance backend test [NAME]` makes one real call with a short sample sentence.
- Speed: Glance only does short, basic text work, so each backend defaults to its fastest,
  cheapest model: claude `haiku`, codex `gpt-6-luna`, gemini `flash-lite`, api (Anthropic)
  `claude-haiku-5-5`. OpenCode and an OpenAI-compatible API run whatever you set (OpenCode:
  its own default). `glance backend list` shows the model each one uses. Change it with
  `glance backend model NAME MODEL` (e.g. `glance backend model codex gpt-6-luna`; for `api`
  it sets `backend.api_model`) and go back with `glance backend model NAME --default`; the
  same lives in `[backend.models]` of the config. A model the tool rejects is retried once
  with the tool's own default.
- Warm claude: with claude chosen, Glance keeps a claude process started and idle, so a call
  skips ~0.9 s of start-up. It costs about 195 MB of memory per idle process (one for checks,
  one for rewrites: up to ~390 MB). Set `warm = false` under `[backend]` in the config to turn
  it off (`true` or leaving it out: on). It is never on for another backend, and it is shut
  down when you switch away from claude, change claude's model or set `warm = false`.
- The onboarding window (tray menu: AI backend, Set up...) does the same with buttons: Copy the
  command, Test, Use this, and API provider, model, base URL and key.
- `glance rewrite --tone friendlier < draft.txt` rewrites stdin to stdout through the chosen AI
  (exit 4 when none is connected). `glance profile [show|set NAME|clear]` shows or changes the
  audience profile of the field you are in.
- The provider processes your text under its own terms; the window says what each tool may keep
  despite Glance's no-session flags.

Codex and OpenCode run with their shell, file and web tools switched off through flags and a
deny-all permission config, but that lock-down could not be confirmed without a billed model
call, so both are marked experimental in `glance doctor`. Text sent to any backend is redacted
first (keys, tokens, cards, private keys).

## Claude Code Ctrl+G

`glance wrapper install` adds a marked `claude()` function to `${ZDOTDIR:-$HOME}/.zshrc`. It sets
`EDITOR`/`VISUAL=glance-edit` for the `claude` process only (the previous editor is kept in
`GLANCE_REAL_EDITOR`); your shell's own `$EDITOR` never changes. `glance wrapper uninstall`
removes the block exactly; `glance wrapper status` prints `installed`, `absent` or `conflict`.
`glance-edit` must be on the PATH of new shells (for example `~/.local/bin`).

## Select-anything and privacy

`Super+Alt+A` rewrites the text you have selected in any app (Copy is the default; "Paste over selection" is optional and absent in terminals). To make Glance never read an app, add its window class to `privacy.app_denylist`: that is the "never read" switch, and select-anything honours it, as it does private browser windows and banking/SSO pages. `[apps.NAME] capture = false` only stops automatic field capture; it does not stop an explicit select-anything request in that app.

## Status and known limits

- Not run yet: the live desktop checklist (real Slack, Discord, Gmail, claude.ai, ChatGPT in
  real profiles). Only the automated tests on a private Xvfb have been run.
- Not run yet: the manual suspend, lock and monitor-hotplug check.
- Firefox write-back is not matrix-tested (the snap build cannot run on a private D-Bus).
  Flatpak and snap apps are not captured.
- Gemini and OpenCode never returned a verified reply in testing; treat them as experimental.
  Codex is experimental too (see above). Default model names are as configured, not re-verified.
- Tested on Python 3.10 / Ubuntu 22.04 on X11 only. Wayland is unsupported; x86_64 only.
- No license has been chosen yet: all rights reserved by default.
- Details: `docs/acceptance/report.md`.
