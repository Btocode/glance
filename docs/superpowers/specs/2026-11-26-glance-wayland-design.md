# Glance on GNOME Wayland (design)

Date: 2026-11-26 · Status: decided by the controller, implemented on this branch

## 1. Why and what

Most users run Ubuntu GNOME on Wayland. Glance worked only on X11 because Wayland forbids
ordinary clients everything the overlay and write-back rely on: windows that float over other
applications without taking focus, global key grabs, synthetic key events, reading which
window is focused and where it is, and owning the clipboard for one paste.

On GNOME the compositor *is* GNOME Shell, and Shell extensions run inside it. So Glance ships a
small GNOME Shell extension, `glance@btocode.github.io`, that does exactly the parts Wayland
forbids, and nothing else. All logic (what to check, what to show, placement, when to apply,
privacy) stays in Python, behind the existing ports.

Supported: GNOME Shell 46–50 on Wayland. Not supported: KDE, Sway and other Wayland
compositors (the installer refuses them with a message saying so). X11 is unchanged.

## 2. Pieces

```
          session D-Bus (local only, never logged)
 glanced  ── io.github.btocode.Glance1  (exported by glanced) ◄── extension calls
   │      ── io.github.btocode.GlanceShell1 (exported by the extension) ◄── glanced/capture call
   │
 glance-capture ── AT-SPI (a11y bus) for text, caret, WINDOW-relative extents
                └─ GlanceShell1 for the focused window, its geometry, keys, clipboard
```

### 2.1 The extension (`extension/`, ESM, shell-version 46–50)

* **Overlay.** The status icon, the collapsed strip, the drawer (tone cards, "Rewrite as…",
  fix list, banners, copy offer), select-anything's panel and the autocomplete ghost are St
  widgets added with `Main.layoutManager.addTopChrome` (input region only where they are
  drawn; never `trackFullscreen`, never focusable). They are rendered from a JSON *scene* that
  glanced sends (`Render`): the serialized `StatusView` / `StripView` / `PanelView` plus the
  colour tokens of `ui/theme.py` (light or dark, Python decides) and the place of each piece.
  The extension is a dumb renderer: it lays out what it is told and reports clicks
  (`Clicked(target, args)`) and its natural sizes (`Sized`) back.
* **Following the window.** The scene carries the focused window's frame rect the places were
  computed against. On `position-changed` the extension shifts every piece by the window's
  delta in the same frame; on `size-changed` it hides them until glanced sends a fresh scene
  (the same rule as X11). Every focus change and geometry change is also sent to glanced
  (`WindowChanged` / `GeometryChanged`) and emitted as the `WindowChanged` signal for capture.
* **Hotkeys.** Configured chords (`[general.hotkeys]`) are bound with
  `Main.wm.addKeybinding` on keys of the extension's own GSettings schema
  (`org.gnome.shell.extensions.glance`, same defaults as X11); glanced tells the extension the
  configured chord (`Grab(name, accel)`). Transient keys (Escape, the use key Alt+Return, Tab
  for the ghost) are grabbed with `global.display.grab_accelerator` only while glanced asks
  (strip/ghost visible) and released after. A press is reported with `Key(name)`.
* **Write-back keys.** A Clutter virtual keyboard
  (`seat.create_virtual_device(KEYBOARD_DEVICE)`). `TypeText`/`Chord` take the pinned window
  id; before every key the extension checks that `global.display.focus_window` is still that
  window and that the shell itself does not hold the keyboard (overview, modal dialog, a
  focused shell entry) and stops otherwise. `KeyState` reports the modifier mask so the engine
  can wait for released modifiers.
* **Clipboard.** `St.Clipboard` for the paste path: save the user's text clipboard, set the
  replacement, Ctrl+V, restore. A clipboard holding non-text data is not restorable: the
  engine then refuses to paste (it types, or offers Copy).
* **Selection** for select-anything: `St.Clipboard` PRIMARY.
* **Test hooks.** Only when gnome-shell runs with `GLANCE_SHELL_TEST=1` (the private headless
  test shell): hide the overview, move/resize a window, describe the overlay, click a piece
  with a virtual pointer, take a screenshot. Never present in a normal session.

### 2.2 Python

New adapters behind the existing ports, nothing else changes shape:

| Port | X11 | GNOME Wayland |
| --- | --- | --- |
| OverlayPort, GhostPort | `ui/overlay_gtk.py`, `ui/ghost_gtk.py` | `ui/overlay_shell.py` |
| HotkeyPort | `ui/hotkeys_xlib.py` | `ui/hotkeys_shell.py` |
| SelectionPort | `ui/selection_xlib.py` | `ui/selection_shell.py` |
| KeyPort | `writeback/keys_xlib.py` | `writeback/keys_shell.py` |
| ClipboardPort | `writeback/clipboard_xlib.py` | `writeback/clipboard_shell.py` |
| windows for AT-SPI | Wnck | `capture/windows_shell.py` |
| transport | — | `shellbus.py` (client), `daemon/shell_link.py` (Glance1) |

* `ui/scene.py` (pure) turns views + places into the JSON scene; it is unit tested.
* `daemon/desktop.py` picks the set: `XDG_SESSION_TYPE=x11` → the X11 adapters (unchanged);
  Wayland on GNOME → the shell adapters. They work whether or not the extension is there:
  while `io.github.btocode.GlanceShell` is not on the bus, they draw nothing, grab nothing and
  write nothing (grammar + tray only), `glance status` reports `shell_extension: missing`,
  and `glance doctor` says how to enable it. When the extension appears (after the next
  login), glanced re-sends hotkeys and the scene: no restart needed.
* **AT-SPI on Wayland.** Screen coordinates from AT-SPI are meaningless on Wayland (clients
  do not know where they are). Capture asks for WINDOW coordinates and adds the origin of the
  focused window's buffer rect from the extension. The active window (pid, class = Wayland
  app id, title, rects) comes from the extension instead of Wnck. Focus tracking is AT-SPI
  focus events filtered by the extension's focused-window pid, exactly as on X11.
  `toolkit-accessibility` must be on (`glance doctor --fix` sets it).
* The privacy gate sees the Wayland app id as `wm_class`; the defaults gain the Wayland ids
  of GNOME's terminals (`org.gnome.terminal`, `org.gnome.ptyxis`, `org.gnome.console`).

### 2.3 Engine contract on Wayland

`WriteBackEngine` is unchanged. Its `KeyPort.focus_target()` is (Meta window id, pid) from the
extension, (0, 0) when the shell holds the keyboard. `type_text` and `chord` carry the pinned
window and the extension refuses keys once it is not focused (`KeysAborted`).
`any_key_down()` is "a modifier is down" (Mutter does not expose the full key state to
extensions); the engine's TypingWatch still stops a write when the user types.

## 3. Privacy

### 3.1 Who may call whom

The extension hands back powers Wayland keeps from ordinary clients (synthetic keys, the
focused window, the clipboard and PRIMARY), so it must not serve any process on the session
bus. Every `GlanceShell1` method is implemented in the async form so the caller's unique
name is known (`extension/peers.js`):

* Accepted callers are the current owners of `io.github.btocode.Glance` (glanced) and
  `io.github.btocode.GlanceCapture` (glance-capture), tracked with `bus_watch_name`, and
  only once `GetConnectionCredentials` says the connection is a process of the shell's own
  uid whose `/proc/<pid>/cmdline` is glanced / glance-capture (the venv's console script, or
  `python -m glance.daemon.main` / `-m glance.capture.main`). The result is cached per unique
  name and dropped when the name changes hands. Everything else gets
  `org.freedesktop.DBus.Error.AccessDenied`; only `Ping` is open (installer, doctor).
* glanced and glance-capture take their names synchronously (`RequestName`) before their
  first call. The extension's calls to glanced and its `WindowChanged` signal go to the
  verified unique names only (unicast), never to whoever holds a name.
* glanced's `Glance1` accepts calls only from the current owner of
  `io.github.btocode.GlanceShell` whose credentials say it is `gnome-shell` of this uid; its
  signal subscription is bound to that name.
* Test hooks (`Test*`, including `TestCall`, which lets the test process use the methods)
  exist only when gnome-shell runs with `GLANCE_SHELL_TEST=1`.

Residual risk: an unsandboxed process of the same user can still start a program whose
command line looks like glanced and take the name first (or simply be glanced run with a
modified source). Such a process can already ptrace the user's processes and read their
files, so the bus is not the weakest point. Sandboxed apps (Flatpak, snap) cannot see or own
these names unless their manifest grants `--talk-name`/`--own-name` for them. A squatter that
holds a Glance name first does not get served; it only denies service (glanced or capture
then cannot use the extension, and `glance doctor` shows the extension as not running).

Typed text and replacements cross the session bus only between glanced/capture and the
extension, both the user's own processes. Neither side logs payloads (the existing
`GLANCE_STRICT_LOG` checks apply to the Python side; the extension never calls `log()` with
text). The paste path's text is visible to clipboard managers on Wayland (no per-client
offer like X11's); it is used only for text the virtual keyboard cannot type.

## 4. Install, doctor, uninstall

* `install.sh`: on Wayland, GNOME Shell 46–50 is accepted; the extension is copied to
  `~/.local/share/gnome-shell/extensions/glance@btocode.github.io`, its schema compiled, every
  file recorded byte for byte in the install manifest, `gnome-extensions enable` run, and the
  user told to log out and in once (Wayland GNOME loads new extensions only in a new session).
  Any other Wayland desktop: refused, naming what is supported.
* `uninstall.sh`: disables and removes the extension (only files that still match the
  manifest).
* `glance doctor`: `session` passes on GNOME Wayland 46–50; a new `shell_extension` check
  (installed / enabled / running); `--fix` enables it and turns `toolkit-accessibility` on.
* Python 3.14 (Ubuntu 26.04) is supported: `requires-python <3.15`, the installer's gate too.

## 5. Testing

* Unit tests for every pure part and every adapter over a fake bus.
* `-m wayland` acceptance tests (skipped unless `GLANCE_WAYLAND_TEST=1` and gnome-shell is
  installed) start a **private** headless GNOME Shell
  (`dbus-run-session -- gnome-shell --headless --wayland --no-x11 --virtual-monitor 1280x800`)
  with a private HOME, XDG dirs, dconf, session bus and a11y bus, the extension enabled in
  that profile only, the GTK test app, and glanced with the fake AI backend. Never the user's
  session.
