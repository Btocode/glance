# Glance on macOS — design

Status: implemented on branch `macos` (first cut; see the report for what ran on a Mac). Companion to
[2026-11-25-glance-design.md](2026-11-25-glance-design.md); everything that document says about
the controller, the grammar pipeline, the AI coordinator, privacy and the write-back contract
holds unchanged. This one says only what is different on a Mac and why.

## Goals and non-goals

- The same product on macOS 13+ (Apple silicon and Intel): status icon, collapsed strip,
  drawer, write-back with verify and one undo step, select-anything, autocomplete, tray menu,
  `glance` CLI, `glance doctor`, one-line installer and an exact uninstaller.
- **No change of behaviour on Linux.** macOS code lives in a new package,
  `src/glance/platform/macos/`; shared files get only small, clearly delimited dispatches.
- Not in the first cut: the Ctrl+G `glance-edit` window, the onboarding window and the usage
  window (the CLI does all three: `glance backend …`, `glance usage`); a signed `.app` bundle.

## Process model and main loop (decision)

Two options were on the table:

- **(a)** keep the Linux process model — `glanced` (controller, overlay, hotkeys, tray) and
  `glance-capture` (accessibility + write-back) — and run both on the Mac's own run loop
  through a small `Clock` implementation;
- **(b)** a headless `glanced` on asyncio/threads plus a separate AppKit UI process speaking
  JSON lines.

**Chosen: (a).** Everything that decides anything in `glanced` already reaches the loop only
through the `Clock` port (`now`, `call_later`, `post`) and the UI only through `OverlayPort`,
`GhostPort`, `HotkeyPort`, `TrayPort`, `NotifierPort` and `SelectionPort`. GLib appears in
exactly three places: `clock.GLibClock`, `daemon/main.py:main()` and `capture/main.py:main()`.
So the Mac needs a `Clock` over the main `NSRunLoop` (`MacClock`: `NSTimer` for `call_later`,
`performSelectorOnMainThread:` for `post`, both in the common run-loop modes so they also fire
while a menu is open), its own two `main()`s that assemble the same parts with AppKit adapters,
and nothing else. Option (b) would have needed a second wire protocol for every overlay,
hotkey and tray call — a far larger surface, with no behaviour gained.

- `glance.platform.macos.daemon.main` is `glanced` on macOS: `NSApplication` with the
  *accessory* activation policy (no Dock icon, never steals focus), the controller, AI,
  features, ctl and capture sockets, child supervision. It imports and reuses the plain glue of
  `daemon/main.py` (`build_ai`, `real_backends`, `SupervisedHarper`, `ConfigReporter`,
  `FakeAiBackend`, `daemon_answers`) and of `daemon/wiring.py`; it does not copy them.
- `glance.platform.macos.capture.main` is `glance-capture` on macOS: a `CFRunLoop` (no
  NSApplication), the AX port feeding the **unchanged** `FocusTracker`, the unchanged
  `WriteBackEngine` over macOS key and clipboard ports, the unchanged `handle_command` /
  `Serialized` / `TypingWatch`. Capture stays a separate process for the same reasons as on
  Linux: an AX call blocks up to its messaging timeout and must never freeze the overlay, and a
  crash in it is restarted by `ChildSupervisor`.
- The only edits to shared entry points are a three-line `if sys.platform == "darwin":` at the
  top of `daemon.main.main()` and `capture.main.main()` that hand off to these modules.

## Coordinates

All shared code (placement, anchors, `Rect` on the wire) uses **global top-left coordinates,
y down**, as X11 does. The Accessibility API uses the same space (origin at the top-left of the
primary display), so AX rects go on the wire unchanged. AppKit places windows in **bottom-left
coordinates, y up**, relative to the primary screen; the conversion happens only inside the
overlay: `cocoa_y = primary_height - (y + h)` (`geometry.to_cocoa` / `from_cocoa`).
`monitors()` returns every `NSScreen.frame` converted to top-left (and `visibleFrame` is used
so the strip never goes under the menu bar or the Dock).

## Capture (Accessibility)

- Focus: `AXUIElementCreateSystemWide()` → `AXFocusedUIElement`; the app is the frontmost
  `NSRunningApplication` (bundle id, localized name, pid). Observers (`AXObserverCreate` per
  app pid, sources on the capture run loop) for `AXFocusedUIElementChanged`, `AXValueChanged`,
  `AXSelectedTextChanged`, `AXFocusedWindowChanged`, `AXWindowMoved`, `AXWindowResized`, plus
  `NSWorkspaceDidActivateApplicationNotification` for app switches. A 250 ms poll of the
  focused element backs the observers up (some apps never post focus changes).
- Text: `AXValue` (string), selection `AXSelectedTextRange` (`AXValueGetValue` of a
  `CFRange`, in UTF-16 units), caret rect `AXBoundsForRange` (parameterized) for the caret's
  character, else the previous one; field rect `AXPosition`+`AXSize`; window rect from
  `AXWindow`. UTF-16 offsets are converted to Python `str` indices at the port boundary
  (`ax_text.utf16_to_py` / `py_to_utf16`), like AT-SPI's filler handling on Linux.
- Writable field = role in `AXTextField`, `AXTextArea`, `AXComboBox`, `AXSearchField`
  (subrole) or a web area's editable element (`AXEditableAncestor` present), and the `AXValue`
  attribute is settable. `role` on the wire is the AT-SPI-style name the gate already knows:
  `AXSecureTextField` (subrole or role) → `password text`, so the **unchanged** privacy gate
  refuses it before any read; a secure field's value is never requested.
- `wm_class` is the **bundle id** (e.g. `com.apple.TextEdit`), `app` the localized name. The
  macOS defaults extend the gate's lists through `[privacy]` defaults merged by the Mac daemon
  and capture (`macos.ax_roles.MAC_PRIVACY`): password managers (1Password, Bitwarden,
  KeePassXC, Keychain Access, LastPass, Dashlane, Enpass), remote desktops (Screen Sharing,
  Microsoft Remote Desktop, AnyDesk, TeamViewer, RustDesk, VNC Viewer, Parallels, VMware
  Fusion, UTM), terminals (Terminal, iTerm2, Ghostty, kitty, Alacritty, WezTerm, Warp) and the
  browsers whose missing URL must gate (`com.apple.Safari`, `com.google.Chrome`,
  `company.thebrowser.Browser`, `com.brave.Browser`, `org.mozilla.firefox`,
  `com.microsoft.edgemac`).
- Browser URL: walk up from the focused element to the `AXWebArea` and read its `AXURL`
  (Safari, Chromium) — `AXDocument` as a fallback. Private windows: the window's `AXTitle`
  (Chrome “Incognito”, Safari's private windows carry no marker in the title — see Limits).
- Chromium / Electron: set `AXManualAccessibility = true` (Electron) and
  `AXEnhancedUserInterface = true` (Chrome) on the app element once per pid, the macOS
  equivalent of `--force-renderer-accessibility`.

## Write-back

The **unchanged** `WriteBackEngine` drives two new ports:

- `MacKeys` (KeyPort) over Quartz events from a private `CGEventSource`
  (`kCGEventSourceStatePrivate`, so the user's physically held modifiers never mix in).
  - `focus_target()` = (window token, pid) of the frontmost app's focused window — the token is
    `CFHash` of the AX window element (stable and equal for the same window), never 0.
  - `type_text` posts one key-down/key-up pair per character with
    `CGEventKeyboardSetUnicodeString` (any script, independent of the keyboard layout) **to the
    pinned pid** (`CGEventPostToPid`), and before every key re-checks that the frontmost pid
    and focused window are still the pinned ones, and that no hardware key went down since the
    last check (`CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState,
    kCGEventKeyDown)`); otherwise `KeysAborted`, rest unsent.
  - `chord` maps the engine's Linux names to Mac ones: `ctrl+v` → ⌘V, `ctrl+z` → ⌘Z,
    `shift+Left` → ⇧←, `BackSpace` → ⌫ (`keyplan.plan_chord`, pure and tested).
  - `any_key_down` / `wait_modifiers_released` read `CGEventSourceFlagsState` and
    `CGEventSourceKeyState` of the HID system state.
- `MacClipboard` (ClipboardPort) over `NSPasteboard.generalPasteboard`: `save` copies every
  item's every type (refusing over 8 MB, like Linux), `set_secret_text` writes the text with
  `org.nspasteboard.TransientType`, `org.nspasteboard.ConcealedType` and
  `org.nspasteboard.AutoGeneratedType` so clipboard managers skip it (the nspasteboard.org
  convention honoured by Maccy, Paste, Alfred, Raycast…), `restore` writes the saved items back
  only if nobody else changed the pasteboard since (`changeCount`).
- Selecting the range uses AX `AXSelectedTextRange` (the engine's “by_atspi” path), falling
  back to ⇧← presses as on Linux. **Edits always go through the keyboard** (typed or one ⌘V),
  never through `AXSelectedText`/`AXValue` writes: those bypass or vary the application's undo
  registration, and the engine's revert depends on ⌘Z being exactly one step. (Controller
  asked to prefer AX writes; this is the one deliberate deviation — see the report.)
- Verification re-reads `AXValue`, exactly as on Linux.

## Overlay (AppKit)

`MacOverlay` implements `OverlayPort` with three borderless `NSPanel`s
(`NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel`), level
`NSStatusWindowLevel`, collection behaviour `canJoinAllSpaces | fullScreenAuxiliary |
stationary | ignoresCycle`, `hidesOnDeactivate = NO`, `becomesKeyOnlyIfNeeded = YES`,
`canBecomeKeyWindow = NO` (a subclass) except the “Rewrite as…” sheet. Placement is the shared
`place_status` / `place_strip` in top-left space, converted at the end.

- Status icon: a 22 px circle, idle (hairline ring), working (turning arc, only while
  `spin`), ready (accent dot + count badge), muted (grey, slashed). A click is `on_chip_click`.
- Strip: mint (`strip` token) rounded card, the corrected sentence as an attributed string
  with changed segments **bold in the accent colour**, a “Use ⌥⏎” button and “⋯”.
- Drawer: four tone cards (label + one-line preview), “Rewrite as…”, the fix list with
  per-issue accept buttons, Fix all, Copy.
- Colours are the tokens of `ui/theme.py` (`LIGHT` / `DARK`), switched by
  `NSApp.effectiveAppearance` (observed through `AppleInterfaceThemeChangedNotification`).
- The overlay follows the target window: capture sends a fresh snapshot on
  `AXWindowMoved`/`AXWindowResized` (registered on the application element, so every window
  of the focused app reports), and the controller re-places the icon and strip from it. macOS
  posts these when a drag ends, so the overlay jumps to the new place rather than gliding.
- Ghost text (autocomplete) is one more click-through panel with the `ghost` colour.

## Hotkeys

`CarbonHotkeys` implements `HotkeyPort` with `RegisterEventHotKey` (Carbon, through `ctypes`:
no Accessibility or Input Monitoring permission needed, the key is consumed, and
`eventHotKeyExistsErr` reports a chord another app holds → `conflict`). Events arrive through
the application event target while `NSApplication` runs.

Modifier names in config keep their spelling: `Super` = ⌘ Command, `Alt` = ⌥ Option,
`Ctrl` = ⌃ Control, `Shift` = ⇧. ⌘⌥ chords collide with app shortcuts everywhere (⌘⌥F find,
⌘⌥Space Finder search), so **the macOS defaults are ⌃⌥ chords**; a chord the user left at its
Linux default is replaced by the macOS default (`hotkeys_mac.mac_hotkeys`):

| action | macOS default |
| --- | --- |
| panel (open the strip/drawer) | ⌃⌥G |
| fix_all | ⌃⌥F |
| revert | ⌃⌥Z |
| pause | ⌃⌥P |
| tone1..tone4 | ⌃⌥1 … ⌃⌥4 |
| complete | ⌃⌥⏎ |
| selection (select-anything) | ⌃⌥A |
| use (strip visible only) | ⌥⏎ |

(⌃⌥Space is not used: it is macOS's “next input source”.) Autocomplete's transient
“accept one word” key, `Control+Right` in the shared code, is registered as ⌥→ on a Mac
(⌃→ switches Spaces; ⌥→ is the Mac's own word-right) — `hotkeys_mac.mac_chord`. Enabled system shortcuts are read
from `com.apple.symbolichotkeys` (`defaults export`, read-only) and reported as `system`, like
GNOME's on Linux. Transient keys — ⌥⏎ use, Esc, Tab for a completion — are registered only
while the strip/drawer/ghost is visible, through the same `PanelEscape` / autocomplete code.

## Tray

`MacTray` (TrayPort) is an `NSStatusItem` with the Linux menu: status line, “Update available
— Update now”, AI backend ▸ (radio list + Set up…), AI usage ▸, “Show suggestions
automatically” (check), Pause/Resume, Quit Glance.

## Selection (select-anything)

`MacSelection` (SelectionPort): `AXSelectedText` of the system-wide focused element (after the
gate's metadata check, never for a secure field), owner/active window from the frontmost app,
pointer from `NSEvent.mouseLocation` converted to top-left.

## Secrets

`KeychainKeyStore` (KeyStore) over the Security framework (PyObjC `SecItemAdd`,
`SecItemCopyMatching`, `SecItemUpdate`, `SecItemDelete`), generic-password class, service
`io.github.btocode.glance.api-key`, account = provider. The key never touches argv.

## Service, paths and logs

- LaunchAgent `~/Library/LaunchAgents/io.github.btocode.glance.plist`: `ProgramArguments`
  = the venv's `glanced`, `RunAtLoad`, `KeepAlive = {SuccessfulExit = false}` (restart after a
  crash, not after Quit), `LimitLoadToSessionType = Aqua`, `ProcessType = Interactive`, logs
  to `~/Library/Logs/Glance/glanced.log`. Loaded with `launchctl bootstrap gui/$UID`.
- **Paths stay XDG-style** (`~/.config/glance/config.toml`, `~/.local/share/glance/…`,
  `~/.local/state/glance/…`): one layout for the CLI, the docs and the installer manifest on
  both platforms. Only the runtime directory changes (there is no `XDG_RUNTIME_DIR`):
  `~/.local/state/glance/run` (mode 0700), short enough for the 104-byte socket path limit.
- Python: a uv-managed CPython 3.12 installed under `~/.local/share/glance/python` (never
  Apple's 3.9, never Homebrew's), venv at `<src>/.venv`, PyObjC from the `macos` extra
  (platform markers: nothing changes on Linux).

## Permissions (TCC)

Accessibility is needed by the process that calls AX and posts events, and macOS ties it to the
**executable** — for a script, the Python interpreter that runs it. `glanced` and its capture
child run the uv-managed interpreter, so that one file is what the user adds under *System
Settings → Privacy & Security → Accessibility*. `glance doctor` prints its exact path (and
`--fix` calls `AXIsProcessTrustedWithOptions` with the prompt from inside the daemon, so the
system dialog names the right binary). The daemon reports `ax_trusted` in `glance status`.
Input Monitoring is not needed (Carbon hotkeys, no event taps).

## Installer

`install.sh` gains a Darwin branch at its very end (bash 3.2-safe; nothing above it runs on a
Mac) that runs `scripts/install-macos.sh` from the checkout, or downloads it next to the
pinned source when piped from curl. `install-macos.sh`: Xcode CLT `git` or a tarball, uv
(installed to `~/.local/share/glance/uv` when absent, no shell-rc edits), uv-managed Python
3.12, venv + `-e .[macos]`, harper-ls (per-arch SHA-256 pin in `fetch-harper.sh`), symlinks in
`~/.local/bin`, LaunchAgent, `glance doctor`. Every created path goes in the same
`install-manifest.json`; `uninstall.sh`'s Darwin branch hands off to
`scripts/uninstall-macos.sh`, which boots the agent out and removes exactly what the manifest
lists.

## Limits (first cut)

- Safari private windows have no title marker; Safari's AX tree does not expose privacy, so
  Safari is gated only by URL and denylists (documented; Chrome/Brave/Arc incognito titles are
  caught).
- Apps that implement no AX text (some games, Java/Swing without the bridge, terminals) are
  not captured; terminals are gated anyway.
- The clipboard cannot be offered to one app only (Linux can): the concealed/transient marks
  are the protection.
- No `.app` bundle yet: the Accessibility grant is on the Python interpreter.
