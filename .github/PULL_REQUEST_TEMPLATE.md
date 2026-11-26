<!-- Thanks for contributing! Please read CONTRIBUTING.md first. Keep examples to made-up text. -->

## What and why

<!-- What does this change, and why? Link the issue it closes: "Closes #123". -->

## How it was tested

<!-- Commands you ran and what you checked by hand (which app, which session type). -->

## Checklist

- [ ] `.venv/bin/pytest` passes (default suite).
- [ ] If this touches capture, write-back, the overlay or hotkeys: `.venv/bin/pytest -m xvfb` passes (private Xvfb, never my own display).
- [ ] I did **not** run `-m desktop` or `-m apps` in a session with real work open, and did not run `paid` tests unless this change needs them.
- [ ] New behaviour has tests; a bug fix has a test that fails without it.
- [ ] `.venv/bin/ruff check` is clean for the files I changed.
- [ ] README / CONTRIBUTING updated if a command, key, setting or install step changed.

### Privacy rules

- [ ] Typed or selected text never goes on argv, to disk, into logs, `glance status` or notifications.
- [ ] Anything sent to an AI backend is redacted first (`privacy/redact.py`).
- [ ] API keys stay in libsecret only.
- [ ] Write-back only goes into the field the edit was computed for.
- [ ] Fixtures, tests and screenshots use synthetic text only.
