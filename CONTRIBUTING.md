# Contributing to Glance

Thanks for taking the time to help. Glance reads what people type in every app on their
desktop, so it holds itself to a high bar on two things: **privacy** and **never corrupting a
field**. This guide explains how to set up a checkout, which tests are safe to run where, how
the code is laid out, and the rules every change has to keep.

- [Ways to contribute](#ways-to-contribute)
- [Development setup](#development-setup)
- [Running the tests](#running-the-tests)
- [Project layout](#project-layout)
- [Coding conventions](#coding-conventions)
- [Privacy rules](#privacy-rules)
- [Adding an AI backend](#adding-an-ai-backend)
- [Commits and pull requests](#commits-and-pull-requests)

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). Security and
privacy problems go through [SECURITY.md](SECURITY.md), not public issues.

## Ways to contribute

- **Report a bug** with the [bug report form](https://github.com/Btocode/glance/issues/new/choose).
  Include `glance doctor` output, never the text you were typing.
- **Try Glance in an app or distro we have not tested** and tell us what worked. The
  [known limits](README.md#status-and-known-limits) list what has not been run yet.
- **Fix a bug or add a feature.** For anything larger than a small fix, open an issue first so
  we can agree on the approach before you write the code.
- **Add or improve an AI backend.** See [Adding an AI backend](#adding-an-ai-backend).

## Development setup

You need the same things Glance needs at run time: an X11 session on x86_64 and the system
Python (`/usr/bin/python3`, 3.10 to 3.13) with GTK 3, AT-SPI and Wnck bindings. The GTK
bindings come from your distribution, which is why the venv is built on the system Python with
`--system-site-packages`.

**1. System packages** (Debian/Ubuntu names; the installer checks for the same list):

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 gir1.2-wnck-3.0 \
    gir1.2-notify-0.7 gir1.2-secret-1
```

For the tests that bring up their own X server and D-Bus session, and the optional tray icon:

```sh
sudo apt install xvfb dbus at-spi2-core gir1.2-ayatanaappindicator3-0.1
```

[`shellcheck`](https://www.shellcheck.net/) is optional: when it is on your `PATH` the tests
also lint `install.sh` and `uninstall.sh`.

**2. A venv and an editable install** with [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/Btocode/glance.git && cd glance
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install -e ".[dev]"
```

**3. The grammar engine.** Fetch `harper-ls` once (SHA-256 pinned, into
`~/.local/share/glance/bin`):

```sh
scripts/fetch-harper.sh
```

**4. Run it.** `glanced` starts the daemon; it starts `glance-capture` and `harper-ls` itself and
restarts them if they die. If you also have an installed Glance, stop it first
(`systemctl --user stop glance.service`) so the two do not fight over hotkeys. To run your
checkout as the service instead, use `./install.sh --from-local`.

## Running the tests

The default run is fast, needs no display of yours, and is what every pull request must pass:

```sh
.venv/bin/pytest
```

The suite is split with markers. The default `addopts` leave out everything that is slow,
touches a real desktop or costs money:

| Marker | What it does | Safe on your desktop? |
| --- | --- | --- |
| *(none)* | unit and adapter tests; X tests start their own private Xvfb | yes |
| `xvfb` | end to end on a private Xvfb + D-Bus session (never your display); slow | yes |
| `desktop` | needs the live X session and AT-SPI bus; presses hotkeys and types | **no**, see below |
| `apps` | drives real third-party apps (browsers, editors) | **no**, see below |
| `paid` | makes real, billed AI calls | costs money |

```sh
.venv/bin/pytest -m xvfb                   # slower end-to-end tests, private Xvfb
GLANCE_PAID=1 .venv/bin/pytest -m paid      # real API calls: you pay for these
```

> [!WARNING]
> **Do not run `-m desktop` or `-m apps` in the session you are working in** unless you know
> what they do. They press global hotkeys and type into windows on your real display. If you
> must, stop your own `glanced` first and keep your hands off the keyboard. Prefer the `xvfb`
> tests: they cover the same paths on a private server.

> [!CAUTION]
> Tests marked `paid` are skipped unless `GLANCE_PAID=1` is set, whatever `-m` expression you
> pass. Each run makes real calls billed to whichever account your CLI or API key uses.

Tests run with `GLANCE_STRICT_LOG=1`, so any log line that would carry text raises instead of
being redacted. A test that fails with `UnsafeLogValue` found a real leak; fix the log call, do
not loosen the check.

Lint with `.venv/bin/ruff check .` (line length 100, target Python 3.10).

## Project layout

```text
src/glance/
├── cli.py            `glance` command line: status, pause, backend, doctor, wrapper, rewrite…
├── config.py         frozen dataclass config, TOML loader, polling hot reload
├── protocol.py       wire protocol between glance-capture and glanced (NDJSON)
├── ipc.py            newline-delimited JSON over unix sockets
├── log.py            text-free logging: only scalars and short safe tokens reach stderr
├── paths.py          XDG locations
├── clock.py          clock port: time, timers, posting to the main loop
├── diffing.py        word-level diff for the rewrite preview (pure)
├── capture/          glance-capture: follows the focused field over AT-SPI, reports snapshots
├── privacy/          the hard gate (metadata only), secret redaction, protected entities
├── grammar/          harper-ls client, issue model, filters, language detection, dictionary
├── daemon/           glanced: controller, scheduler, AI coordinator, ctl socket, supervisor
├── backends/         AI backends (claude, codex, gemini, opencode, api), prompts, budget
├── writeback/        put one edit into the focused field over XTEST or the clipboard, verify it
├── ui/               GTK overlay strip and panel, tray, onboarding, hotkeys, placement, theme
├── features/         autocomplete, select-anything, send-guard, Ctrl+G edit session, sharpener
├── audience/         audience profiles and which one applies to a field
├── doctor/           `glance doctor`: checks and opt-in, reversible fixes
├── integrations/     the zsh `claude` wrapper for Ctrl+G
└── edit_shim/        glance-edit, the $EDITOR shim Claude Code calls

tests/                default suite, plus tests/desktop/ and tests/acceptance/ (opt-in markers)
scripts/              fetch-harper.sh, fixture and test-corpus generators
packaging/            the systemd user unit
docs/                 acceptance report, design specs and plans, README assets
```

## Coding conventions

These are what the existing code does; please match them.

- **Ports and adapters.** Logic that decides things is pure and has no GTK or X in it
  (`*_model.py`, `controller.py`, `scheduler.py`, `tracker.py`, `engine.py`). It talks to the
  outside through small protocols in `ports.py`; the real adapters are `*_gtk.py`, `*_xlib.py`
  and `atspi_port.py`. Pure code is tested with the fakes in `tests/fakes.py`; adapters are
  tested on a private Xvfb.
- **Every module starts with a docstring** that says in one line what it is and what it never
  does ("No GTK.", "Nothing here ever touches the disk.").
- `from __future__ import annotations`, type hints everywhere, frozen dataclasses for values.
- Python 3.10 syntax (`X | None`, `match` is fine); no third-party runtime dependencies beyond
  those in `pyproject.toml` without discussing it first.
- Log with `glance.log.log_event(event, key=value, …)`, never `print` or `logging` with text.
- Fail closed. When in doubt whether text may be read, sent or written, don't.
- A test with every change. Bugs get a regression test that fails before the fix.

## Privacy rules

These are hard rules. A pull request that breaks one will not be merged, however useful it is.

1. **Typed text never goes on argv.** Not the draft, not a "Rewrite as…" instruction, not a
   selection. CLI backends get it on stdin, the API backend in the request body.
   `tests/test_backend_argv_privacy.py` enforces this for every backend.
2. **Typed text never goes to disk.** Caches are in memory. The personal dictionary and ignored
   rules (`grammar/store.py`) are the only files Glance keeps next to the user's text.
3. **Typed text never goes to logs**, `glance status`, notifications or exceptions that are
   logged. Use `log_event` with lengths, hashes (`glance.log.fingerprint`) and safe tokens.
4. **Redact before any AI call.** Everything sent to a backend goes through
   `privacy/redact.py` first (keys, tokens, cards, private keys) and is unshielded after.
5. **The privacy gate decides from metadata alone** (`privacy/gate.py`): app, window class and
   title, URL, role and states. It never sees the text and fails closed. Deny lists, private
   windows, password fields and banking/SSO pages must keep working.
6. **API keys live only in libsecret** (`backends/keystore.py`), never in the config, on argv
   or in the logs.
7. **Write-back only into the field the edit was computed for.** Check that it landed, undo it
   if not (`writeback/engine.py`), and never interleave with the user's typing.

Test fixtures and screenshots use synthetic text only. Never commit real messages, names,
e-mail addresses or anything secret-shaped (the secret corpus is generated at test time by
`scripts/gen-secret-corpus.py` for this reason).

## Adding an AI backend

1. **Read the contract** in [`src/glance/backends/base.py`](src/glance/backends/base.py): the
   `Backend` protocol (`analyze`, `rewrite`, `complete`, `cancel`, `health`), the `Analysis`
   types, `BackendError` kinds, and `BaseBackend`, which implements the shared analyze/rewrite
   flow on top of two hooks, `_run_json` and `_run_stream`.
2. **Start from the closest existing backend.**
   [`claude.py`](src/glance/backends/claude.py) is the reference CLI backend (structured output
   plus streaming); [`codex.py`](src/glance/backends/codex.py) shows a CLI with no streaming
   deltas; [`api.py`](src/glance/backends/api.py) talks HTTPS directly. Run CLIs through
   `backends/proc.py` (own session, empty cwd, hard timeouts, cancel), and switch off any
   tools, file access or session saving the CLI offers.
3. **Register it** where the other names are listed: `CONNECT`/`SIGN_IN` in `base.py`,
   `DEFAULT_MODELS`/`CLI_MODEL_BACKENDS` in `config.py`, `BACKENDS` in `cli.py`,
   `ORDER`/`TITLES` and the data-retention note in `ui/onboarding_model.py`, the backend table
   in `daemon/main.py`, and the health check in `doctor/checks.py`.
4. **Test it** with a fake runner, like `tests/test_claude_backend.py`, and add it to
   `tests/test_backend_argv_privacy.py`. A test that makes a real call must be marked `paid`.
5. **Document it** in the README's "Connect an AI" table. Mark it *experimental* until a real
   reply has been verified.

## Commits and pull requests

- Keep each pull request to one change, and fill in the
  [template](.github/PULL_REQUEST_TEMPLATE.md).
- Commit messages follow the style already in the history, a type prefix and a short
  imperative summary:

  ```text
  fix: select-anything refuses while a password field is focused
  feat: suspend/lock/hotplug handling and resilience tests
  test: paid tests run only with GLANCE_PAID=1, whatever -m selects
  ```

  Types in use: `feat`, `fix`, `perf`, `test`, `docs`, `chore`, `revert`. Explain *why* in the
  body when it is not obvious.
- Before you open the pull request: `.venv/bin/pytest` passes, `.venv/bin/ruff check` is clean
  for the files you touched, and, if you changed capture, write-back or the overlay,
  `.venv/bin/pytest -m xvfb` passes too.
- Update the README when you change something a user sees: a command, a key, a setting.

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
