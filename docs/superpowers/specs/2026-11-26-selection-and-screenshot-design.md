# Glance: Selection actions and Glance Shot — design and plan

Status: approved by the maintainer (2026-11-26), with the decisions below.

## Why

Glance already fixes what you type. The same assistant is useful for text you **read** (a
paragraph in a browser, a log line, an email) and for things that are **not text at all**
(an error dialog, a chart, a screenshot of code). Two features, each switchable on and off:

1. **Selection actions** — select text anywhere and a small Glance button appears next to it;
   click it for Explain, Summarize, Translate, Fix grammar, Rewrite, Reply, Copy.
2. **Glance Shot** — a shortcut to capture a screen region and act on it: copy, save, extract
   the text (OCR), explain or ask about it, translate it.

## Goals / non-goals / anti-goals

- Goals: one-gesture help on any selected text or screen region, on X11, GNOME Wayland and
  macOS; each feature independently enabled/disabled (tray, config, `glance` CLI); the same
  slate/emerald look and keyboard-first UX as the rest of Glance.
- Non-goals (first release): annotation/drawing on screenshots, screen recording, scrolling
  capture, history/gallery, cloud upload or share links.
- Anti-goals: reading text the user did not ask about; screenshots on disk the user did not
  save; sending images to an AI without the user clicking an AI action; popups that steal
  focus or appear while the user is typing.

## 1. Selection actions

### Behaviour
- When a selection of ≥ 2 words settles (~350 ms, mouse button up), a **small Glance mark**
  appears just above-right of the selection end. Nothing is read yet.
- Hover or click the mark → a compact action bar: **Explain · Summarize · Translate ·
  Fix grammar · Rewrite… · Reply… · Copy** (+ `⋯` for tone chips). The text is read only now.
- The result opens in a panel in the Glance Assistant style (same as the field panel):
  result text, **Copy** (default), **Replace selection** (only in an editable field we can
  verify — reuses select-anything's verified write-back), **Ask a follow-up** input.
- The keyboard path stays: **Super+Alt+A** (macOS ⌃⌥A) opens the action bar on the current
  selection without the floating mark.
- The mark hides on: selection cleared, typing, scroll, focus change, Esc, or 6 s idle.

### Actions (prompts live in backends/prompts.py, same shielding/redaction as today)
| Action | Local / AI | Notes |
|---|---|---|
| Fix grammar | local Harper first, AI if connected | reuses select-anything |
| Explain | AI | "explain simply, in the reader's language"; code/log aware |
| Summarize | AI | bullet list for > 120 words |
| Translate | AI | target = `[selection] translate_to` (default: English; Banglish → English/Bangla) |
| Rewrite… / tones | AI | existing tone chips |
| Reply… | AI | drafts a reply in the user's tone (audience profile of the app) |
| Copy | local | plain copy |

### Privacy
- The floating mark is driven by **selection-changed events without reading text**
  (X11 XFixes PRIMARY owner change; AT-SPI `text-selection-changed`; GNOME extension via
  AT-SPI; macOS `AXSelectedTextChanged`). Text is read only when an action is clicked.
- The existing gates apply before showing the mark at all: app denylist, private/incognito
  windows, sensitive URLs, password fields, terminals (opt-in only).
- Selections go through the same redaction (`shield`) before any AI call.

### Settings
```toml
[selection]
enabled = true          # the floating mark; Super+Alt+A works even when false
min_words = 2
translate_to = "English"
terminals = false       # show the mark in terminals
```
Tray: "Selection actions" check item. CLI: `glance features selection on|off`.

## 2. Glance Shot

### Behaviour
- **Super+Alt+C** (macOS ⌃⌥C) — "capture". Or tray → "Take a Glance Shot".
- Region picker: screen dims, drag to select a rectangle (Enter = full screen, Space =
  window under the pointer, Esc = cancel). A size label follows the cursor.
- After release, a **Shot bar** appears beside the region: **Copy · Save · Extract text ·
  Explain · Ask… · Translate · Close**.
  - Copy: image to clipboard. Save: to `~/Pictures/Glance/` (configurable), opens nothing.
  - Extract text: **local OCR**, result in the Glance panel with Copy / Fix grammar /
    Translate / Explain on the extracted text (then the selection actions apply).
  - Explain / Ask…: sends the **image** to a vision-capable AI (see Backends).
  - Translate: OCR locally, then translate the text (no image upload needed).
- The captured image lives in memory only, until Save/Copy; it is dropped when the bar
  closes.

### Platform capture
| Platform | Region picker | Capture |
|---|---|---|
| X11 | Glance's own full-screen override-redirect overlay (GTK, no focus steal) | XGetImage of the root window (python-xlib) |
| GNOME Wayland | drawn by the Glance Shell extension (St actors in the shell) | `Shell.Screenshot.screenshot_area` from inside the extension — no portal dialog; served only to verified glanced (same peer check as today) |
| macOS | `screencapture -i -x -c`-style interactive region via ScreenCaptureKit / `CGWindowListCreateImage` | needs the **Screen Recording** permission (one-time, like Accessibility; asked only when Shot is first used) |

### OCR (local, no network)
- Linux: the `ocrs` Rust crate inside **glance_core** (pure Rust, ~10 MB models fetched and
  SHA-pinned on first use) — the one place where adding Rust clearly pays off (no system
  packages, same wheel on every distro). Fallback: `tesseract` if installed.
- macOS: Apple's Vision framework (`VNRecognizeTextRequest`) — built in, no download.

### AI on images (only on Explain / Ask)
- Supported: API key backend (Anthropic/OpenAI-compatible vision), claude CLI via
  `--input-format stream-json` image blocks (verify), codex `exec -i <image>` (verify).
  Others: the action shows "needs a vision-capable AI — Extract text works locally".
- **Images cannot be redacted.** The first time, a one-line confirm: "This sends the image
  to <backend>. Anything visible in it is sent." with "Always allow" — and the region picker
  refuses when a password field or denylisted app is under the region (best effort).
- Counts toward the usage ledger and the monthly cost limit like any AI call.

### Settings
```toml
[shot]
enabled = true
save_dir = "~/Pictures/Glance"
ocr = "auto"            # auto | ocrs | tesseract | off
ai_images = "ask"       # ask (confirm once) | allow | never
```
Tray: "Glance Shot" check item + "Take a Glance Shot". CLI: `glance shot` (start a capture),
`glance features shot on|off`.

## Shortcuts (checked against the conflict tables in docs/shortcuts.md)
| Action | Linux | macOS |
|---|---|---|
| Selection actions on current selection | Super+Alt+A (exists) | ⌃⌥A (exists) |
| Glance Shot | Super+Alt+C (to verify free) | ⌃⌥C (to verify free) |

## Enable / disable (single place)
- `[features]`-style switches above; a tray submenu **Features ▸** with check items
  *Check while typing / on shortcut*, *Selection actions*, *Glance Shot*, *Autocomplete*.
- `glance features` lists them; `glance features <name> on|off` toggles; changes apply live.
- Disabled = no event listeners, no hotkey grab, no overlay windows created.

## Approaches considered
1. **Extend select-anything + add a Shot module (recommended).** Reuses the privacy gates,
   verified write-back, panel, AI plumbing and renderers; only the floating mark, the action
   bar, the region picker and OCR are new.
2. Separate "Glance Tools" app/process. Cleaner isolation, but duplicates the overlay,
   hotkeys, privacy gates and AI wiring on three platforms — rejected.
3. Rely on OS tools (GNOME screenshot UI, macOS ⌘⇧4) and only post-process the file.
   Least code, but no in-place action bar, files on disk by default, and on Wayland the
   portal shows its own dialog every time — rejected as the primary path (kept as a fallback
   where the extension is not active).

## Implementation plan (tasks, in order)

Each task: unit tests for the shared model, renderers on X11 (private Xvfb shots), GNOME
(private headless shell, one `-m wayland` run), macOS compile + one run on the Mac when it is
free; default suite once per task (`-n 10`).

**A. Feature switches** — `[features]`/`[selection]`/`[shot]` config, tray "Features ▸",
`glance features` CLI, live apply. *(small)*

**B. Selection detection without reading** — selection-settled events per platform
(XFixes, AT-SPI, extension, AX), gated by the privacy gate; emits geometry only. *(medium)*

**C. Floating mark + action bar** — shared model (`selection_bar.py`), three renderers,
hover/click rules, hides on typing/scroll/focus. *(medium)*

**D. Selection actions** — Explain/Summarize/Translate/Reply prompts, result panel with
Copy / Replace (verified) / follow-up; reuse select-anything session. *(medium)*

**E. Region picker** — X11 overlay, GNOME extension actors, macOS interactive capture;
Enter/Space/Esc; returns a PNG in memory. *(large)*

**F. Shot bar + Copy/Save** — shared model, renderers, clipboard image set (X11 owner,
St.Clipboard, NSPasteboard), save dir. *(medium)*

**G. Local OCR** — `ocrs` in glance_core (core 0.3.0, models fetched + SHA-pinned on first
use), macOS Vision, tesseract fallback; Extract text → panel → selection actions. *(medium)*

**H. Vision AI** — image support in API backend (+ claude/codex where verified), the
one-time confirm, usage/cost accounting, "needs a vision AI" fallback. *(medium)*

**I. Docs + release** — README sections, docs/shortcuts.md, screenshots, core-v0.3.0 wheels.

Rough size: A–D (selection) ≈ one focused day of agent work; E–H (Shot) ≈ one to two.
Ship selection actions first (A–D), then Glance Shot (E–H).

## Decisions (maintainer, 2026-11-26)
1. The floating selection mark is **on by default**.
2. Translate defaults to **English**; any other language is done by the AI model — the action
   bar offers "Translate to…" (free text, remembered), and the prompt names the target
   language explicitly.
3. Explain / Ask about an image ship in the first release; an image is sent **only** when the
   user clicks one of those Glance options (with the one-time notice). Nothing else uploads.
