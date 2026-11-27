# Changelog

All notable changes to Glance are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Glance uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) (0.x: anything may still change).

Releases before 0.4.0 were not tagged; their sections summarise the main branch at that point.
The grammar engine `glance_core` is versioned and released on its own (`core-v*` tags).

## [Unreleased]

### Added
- `glance start` starts Glance again after Quit in the tray (systemd user unit on Linux, the
  login agent on macOS) and waits until it answers.
- Glance Shot ▸ Mark: draw on a shot (pen, box, arrow, highlighter, undo) before it is
  copied, saved or sent to the AI.
- Glance Shot ▸ Text ▸ Exact text (AI): the image read word for word by a vision AI, on
  that click (the local reading stays the instant, private one).
- Translate offers the popular languages as one-click choices (and Other…), in Glance Shot
  and in the selection bar's Translate to….

### Changed
- Glance Shot starts as a slim floating toolbar beside the region, like the selection bar;
  it grows into the card under that toolbar only when there is something to show, and the
  toolbar never moves. Ask is typed in the card itself instead of a window of its own.

### Fixed
- Selection actions never appeared in Chrome, Electron apps and Firefox on X11: their
  selection is owned by a hidden helper window the privacy gate could not tie to the app.

## [0.5.0] - 2026-11-27

### Added
- Settings ▸ General ▸ Checked by: "AI (fast model)" (default) or "Local + AI"
  (`[general] checker`).
- Glance Shot outlines the captured region while its card is up.

### Changed
- The check is the AI's (`[general] checker = "ai"`, the new default): with an AI connected
  the strip shows the loader until the fast model answers, and the local checker's findings
  show only when no AI answers. `checker = "local"` (Settings ▸ General ▸ Checked by) keeps
  the old local-first behaviour.
- Glance Shot is one card beside the captured region, which stays outlined: Copy image and
  Save in its header, then Text · Explain · Translate · Ask… as tabs, and the result in the
  same card. Nothing closes and reopens elsewhere; going back to a tab shows its result
  again without asking again. Explain without a vision AI explains the text read locally.
- Selection actions' answer (Explain, Translate…) opens under the selected line instead of
  over it, and stays in place for follow-ups.
- The status icon's toolbar opens on right-click only, not on hover.

### Fixed
- A dark band around Glance's floating windows on X11: their shadow was larger than the room
  left for it and was cut off at the window's edge.

## [0.4.0] - 2026-11-27

The first tagged release.

### Added
- A Settings window (tray → Settings…, or `glance settings`): check mode, correction
  strength, features, AI choice, API key, monthly cost limit, shortcuts (click to change,
  with conflict checks), per-app on/off, privacy and About.
- Correction strength: `[general] correction = "minimal"` (default, never rewords) or
  `"fluent"` (also fixes awkward grammar with a small rephrase).
- An accuracy test bank (362 cases) and `scripts/eval-accuracy.py`; local-only accuracy is
  checked in CI.
- The icon toolbar: hover or right-click the Glance icon for Check, tones, Rewrite and ⋯.
- Versions: `glance --version`; the version is shown by `glance doctor`, `glance status` and
  the tray menu.
- Releases: each `v*` tag builds Glance's wheel and source archive and publishes them on
  GitHub Releases with SHA-256 sums and these notes.
- `glance update` installs the latest release; `glance update --main` follows the main
  branch. The update check compares against the latest release ("Glance 0.4.0 → 0.5.0").
- The installer takes `--ref latest|main|vX.Y.Z` (or `GLANCE_REF`). The default, `latest`,
  installs the newest release, or the main branch while none exists.
- `glance setup` (and `glance setup --undo`): the per-user part of an install, for Glance
  installed from a package.
- Packaging recipes for the AUR (`glance-writing-assistant`), Homebrew (`Btocode/glance`
  tap) and a `.deb` that installs into `/opt/glance`.
- `glance report`: a diagnostics file to attach to an issue. It holds versions, the system,
  doctor results, AI sign-in states, feature switches, the redacted config and a filtered log
  tail. It never holds typed text, the clipboard, window titles, URLs, keys or user names.
- A demo GIF in the README.

### Changed
- AI checks answer in about 1.5 s instead of about 6 s (low-effort model calls, a ready
  claude process, streamed results). The corrected text fills in as it arrives.
- Better corrections: AI exact matches on the test bank 87% → 98%, no false corrections of
  correct, protected or Banglish text; local fixes stay where the AI left the text unchanged.
- The local checker leaves style suggestions, chat words, British spellings and code alone.
- Glance Shot can capture any region of the screen; nothing is off limits.

## [0.3.0] - 2026-11-26

### Added
- Shortcut mode: check the focused field on Super+Alt+G (⌃⌥G on macOS), with a hover menu
  and conflict-free default shortcuts.
- Selection actions: a mark beside selected text with Explain, Summarize, Translate (and
  Translate to…), Fix grammar, Rewrite…, Reply… and Copy, on X11, GNOME Wayland and macOS.
- Glance Shot (Super+Alt+C / ⌃⌥C): pick a screen region, read its text with local OCR, copy,
  save, or ask the AI about it. An image goes to a vision AI only on Explain or Ask, after a
  one-time notice.
- `glance features` and a tray Features submenu: one switch per feature, applied live.

### Changed
- A new look on every desktop: slate and emerald, light and dark. Tones are picked with a
  click (or 1–4) and applied with Apply. The logo is the loader.
- The status icon's toolbar replaces its vertical hover menu.
- An explicit check asks the AI first, and its analysis no longer times out.
- glanced quits within 3 s, never holds a logout and never leaves a key held down.
- First run on macOS asks for Accessibility itself, and an AI is chosen automatically.
- The installer gives up on a stalled download instead of hanging.

## [0.2.0] - 2026-11-26

### Added
- GNOME on Wayland (GNOME Shell 46–50) through Glance's own shell extension: overlay,
  hotkeys, typing and clipboard.
- macOS 13 and later: Accessibility capture, AppKit overlay, menu-bar icon, Keychain and a
  LaunchAgent; `install.sh` hands off to a macOS installer.
- `glance update` and a daily update check with a tray "Update now" item.
- `glance usage` and a tray submenu: AI calls, tokens and cost, with a monthly cost limit.
- The in-process grammar engine (glance_core: Harper in Rust) with pinned prebuilt wheels;
  harper-ls remains the fallback. Secret detection runs there too.
- Python 3.14 support.
- Brand: logo, app icons and tray icon.

### Changed
- The suggestion strip: the default sentence only corrects, every change is highlighted and
  counted, and suggestions cover the whole text. `[ai] max_words` caps what the AI reads.
- The status icon sits by the field and the strip opens on click (`[ui] suggestions`).
- The overlay appears only beside an editable field; setup never opens by itself.
- AI command-line tools are found where the login shell would find them.

## [0.1.0] - 2026-11-26

The first public version, for Linux on X11.

### Added
- Grammar and spelling checks as you type in any app, with harper-ls running locally.
- An inline suggestion strip with tone rewrites (friendlier, more direct, shorter,
  professional), a streamed preview, and an in-place apply that one Ctrl+Z undoes.
- Autocomplete as ghost text, accepted with Tab or one word at a time.
- Select-anything rewrite and the Ctrl+G edit window for Claude Code (an opt-in `claude`
  wrapper; `$EDITOR` is never changed).
- AI backends: the Claude, Codex, Gemini and OpenCode command-line tools, or an API key kept
  in the system keyring. The fastest, cheapest model is chosen per backend.
- Privacy: a capture gate for password fields, terminals and denied apps; secrets are
  redacted before any text reaches an AI; logs never hold text.
- `glance doctor` (with `--fix` and `--undo`), `glance status`, `pause`/`resume`, `backend`,
  `rewrite` and `profile`.
- An installer and uninstaller that record and reverse exactly what they change, and a
  systemd user unit.
- MIT licence, contributing guide, code of conduct and security policy.

[Unreleased]: https://github.com/Btocode/glance/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Btocode/glance/releases/tag/v0.5.0
[0.4.0]: https://github.com/Btocode/glance/releases/tag/v0.4.0
[0.3.0]: https://github.com/Btocode/glance/commit/597c07d
[0.2.0]: https://github.com/Btocode/glance/commit/68f756f
[0.1.0]: https://github.com/Btocode/glance/commit/27fab6d
