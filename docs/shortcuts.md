# Glance's shortcuts and the desktops' own

Glance's default chords were picked so that none of them takes a shortcut the desktop (or a
common window manager) already uses. This page lists what was checked. The same tables are in
the code (`src/glance/ui/shortcut_table.py`), and a test keeps every default chord off them.

At run time Glance also reads the live desktop's shortcuts (GNOME through `gsettings`, macOS
through `com.apple.symbolichotkeys`). It never grabs one of those and says so in a notification,
`glance status` and `glance doctor`. A chord another program already holds is reported the
same way. Change a chord under `[general.hotkeys]` in `~/.config/glance/config.toml`; a chord you
do not set keeps its default.

## Defaults

| Action | `[general.hotkeys]` key | Linux (X11 and GNOME Wayland) | macOS |
| --- | --- | --- | --- |
| Check the field now (opens the strip) | `check` | `Super+Alt+g` | ⌃⌥G |
| Open the strip and drawer (tone rewrites, "Rewrite as…") | `panel` | `Super+Alt+space` | ⌃⌥O |
| Fix all safe grammar | `fix_all` | `Super+Alt+f` | ⌃⌥F |
| Undo Glance's last change | `revert` | `Super+Alt+z` | ⌃⌥Z |
| Pause / resume | `pause` | `Super+Alt+p` | ⌃⌥P |
| Pick tone 1–4 (Alt+Enter applies it) | `tone1` … `tone4` | `Super+Alt+1`, `Super+Alt+2`, `Super+Alt+3`, `Super+Alt+4` | ⌃⌥1 … ⌃⌥4 |
| Ask for a completion | `complete` | `Super+Alt+Return` | ⌃⌥⏎ |
| Rewrite the selection in any app | `selection` | `Super+Alt+a` | ⌃⌥A |
| Use the strip's suggestion (only while it shows one) | `[ui] use_key` | `Alt+Return` | ⌥⏎ |

On macOS `Super` is ⌘, `Alt` ⌥ and `Ctrl` ⌃. A chord left at its Linux default becomes the
macOS default above; a chord you set is used as you wrote it.

What changed in this release:

- `check` is new (`Super+Alt+g` / ⌃⌥G).
- On macOS the panel moved from ⌃⌥G to ⌃⌥O, so ⌃⌥G (G for Glance) is the check on every
  platform. A config that sets `panel` itself keeps its own chord.
- Select-anything stays off `Super+Alt+s`. GNOME uses that chord to turn the screen reader (Orca)
  on and off.

## Linux: what was checked

GNOME 46 (Ubuntu 24.04), read with `gsettings list-recursively` on these schemas:
`org.gnome.desktop.wm.keybindings`, `org.gnome.shell.keybindings`, `org.gnome.mutter.keybindings`,
`org.gnome.mutter.wayland.keybindings`, `org.gnome.settings-daemon.plugins.media-keys`,
`org.gnome.desktop.a11y.*` and the Ubuntu dock (`org.gnome.shell.extensions.dash-to-dock`).
These are all the `Super+Alt` chords GNOME binds:

| Chord | GNOME uses it for |
| --- | --- |
| `Super+Alt+s` | screen reader (Orca) on/off |
| `Super+Alt+8` | zoom (magnifier) on/off |
| `Super+Alt+equal` / `Super+Alt+minus` | zoom in / out |
| `Super+Alt+Up` / `Super+Alt+Down` | overview up / down |
| `Super+Alt+Left` / `Super+Alt+Right` | workspace left / right |
| `Super+Shift+Alt+Left` / `…Right` | move the window one workspace |

Other GNOME and Ubuntu defaults that a `Super` or `Ctrl+Alt` chord could clash with, none of
which Glance uses: `Super+space` (input source), `Super+a` (apps), `Super+v` (notifications),
`Super+l` (lock), `Super+d` (desktop), `Super+s` (quick settings), `Super+1…9` and their
`Shift`/`Ctrl` forms (Ubuntu dock), `Super+q` (dock numbers), `Ctrl+Alt+t` (terminal),
`Ctrl+Alt+Left/Right` (workspaces), `Ctrl+Alt+d`, `Ctrl+Alt+Delete`, `Alt+space` (window menu),
`Alt+F2`.

KDE Plasma and Xfce defaults that were checked (from their documented defaults, not from a live
session): Plasma `Meta+Alt+k` (next keyboard layout), `Meta+Alt+l` (some versions), `Meta+e`,
`Meta+p`, `Alt+F1`, `Ctrl+Alt+Escape`; Xfce `Super+r`, `Super+t`, `Super+p`, `Ctrl+Alt+l`,
`Ctrl+Alt+Escape`. None of Glance's chords is among them.

Result: no Glance default collides. `Super+Alt+g` (check) is free on all of them.

## macOS: what was checked

System shortcuts (System Settings → Keyboard → Keyboard Shortcuts) and the standard app ones:

| Chord | macOS uses it for |
| --- | --- |
| ⌘Space / ⌥⌘Space | Spotlight / Finder search |
| ⌃Space / ⌃⌥Space | previous / next input source |
| ⌃↑ ⌃↓ ⌃← ⌃→ | Mission Control, app windows, spaces |
| ⇧⌘3 ⇧⌘4 ⇧⌘5 (and with ⌃) | screenshots |
| ⌥⌘F5, ⌘F5 | accessibility shortcuts, VoiceOver |
| ⌃⌘Q, ⌃⌘F, ⌃⌘Space | lock screen, full screen, emoji |
| ⌃⌥⌘8, ⌥⌘8, ⌥⌘= ⌥⌘- | invert colours, zoom |
| ⌥⌘D, ⌥⌘Esc | Dock hiding, force quit |
| ⌘Tab, ⌘\`, ⌘H, ⌥⌘H, ⌘M, ⌘Q, ⌘W, ⌘, | app and window switching, hide, quit, close, settings |
| ⌃F2 ⌃F3 ⌃F7, ⇧⌘/ | keyboard navigation, Help |
| Globe/fn E, Fn Fn | emoji, dictation (not chords Glance could take) |

Glance uses ⌃⌥ chords because ⌘⌥ chords clash with app shortcuts everywhere. None of the ⌃⌥
defaults above is a macOS system shortcut. **VoiceOver** uses ⌃⌥ as its "VO" modifier while it
runs, so with VoiceOver on, pick other chords under `[general.hotkeys]`.

## When a chord is taken anyway

Desktops differ and users add their own shortcuts. Glance never takes a chord the desktop has
bound (GNOME and macOS are read live). It reports each chord it could not bind:

- a notification when glanced starts or the config changes,
- `glance status` (`hotkeys_unavailable`),
- `glance doctor` → **Hotkeys**: the chord, and why (a desktop shortcut, or another program holds
  it).
