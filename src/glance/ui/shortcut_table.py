"""The desktops' own shortcuts Glance's default chords were checked against.

docs/shortcuts.md is the readable version of these tables; a test keeps every default chord
off them (and the defaults free of duplicates). Chords use Glance's spelling: on macOS
`Super` is ⌘, `Alt` ⌥, `Ctrl` ⌃. At run time glanced also reads the live desktop's shortcuts
(gsettings / com.apple.symbolichotkeys) and refuses to take any of those, whatever is here.
"""
from __future__ import annotations

# (chord, owner). GNOME entries are as `gsettings list-recursively` printed them on GNOME 46
# (Ubuntu 24.04) for org.gnome.desktop.wm.keybindings, org.gnome.shell.keybindings,
# org.gnome.mutter.keybindings, org.gnome.mutter.wayland.keybindings and
# org.gnome.settings-daemon.plugins.media-keys; the rest are the documented defaults.
LINUX_RESERVED: tuple[tuple[str, str], ...] = (
    # GNOME (gnome-settings-daemon media-keys)
    ("Super+Alt+s", "GNOME: screen reader (Orca) on/off"),
    ("Super+Alt+8", "GNOME: zoom (magnifier) on/off"),
    ("Super+Alt+equal", "GNOME: zoom in"),
    ("Super+Alt+minus", "GNOME: zoom out"),
    # GNOME Shell / Mutter
    ("Super+Alt+Up", "GNOME Shell: overview up"),
    ("Super+Alt+Down", "GNOME Shell: overview down"),
    ("Super+Alt+Left", "GNOME: workspace left"),
    ("Super+Alt+Right", "GNOME: workspace right"),
    ("Super+Shift+Alt+Left", "GNOME: move window to the workspace on the left"),
    ("Super+Shift+Alt+Right", "GNOME: move window to the workspace on the right"),
    ("Ctrl+Alt+Left", "GNOME: workspace left"),
    ("Ctrl+Alt+Right", "GNOME: workspace right"),
    ("Ctrl+Alt+d", "GNOME: show desktop"),
    ("Ctrl+Alt+t", "GNOME / Ubuntu: terminal"),
    ("Ctrl+Alt+Delete", "GNOME: log out"),
    ("Ctrl+Alt+Tab", "GNOME: switch system controls"),
    ("Alt+space", "GNOME: window menu"),
    ("Alt+F2", "GNOME: run a command"),
    ("Super+space", "GNOME: next input source"),
    ("Super+Shift+space", "GNOME: previous input source"),
    ("Super+a", "GNOME / Ubuntu dock: show apps"),
    ("Super+v", "GNOME: notification list"),
    ("Super+l", "GNOME: lock screen"),
    ("Super+d", "GNOME: show desktop"),
    ("Super+s", "GNOME: quick settings"),
    ("Super+Tab", "GNOME: switch applications"),
    ("Super+Shift+Return", "GNOME / Ubuntu: new terminal (dock)"),
    # Ubuntu dock (dash-to-dock): Super+1..9 and their Shift / Ctrl forms
    *((f"Super+{n}", "Ubuntu dock: open app") for n in range(1, 10)),
    *((f"Super+Shift+{n}", "Ubuntu dock: new window of app") for n in range(1, 10)),
    *((f"Super+Ctrl+{n}", "Ubuntu dock: app") for n in range(1, 10)),
    ("Super+q", "Ubuntu dock: show app numbers"),
    # KDE Plasma defaults
    ("Super+Alt+k", "KDE Plasma: next keyboard layout"),
    ("Super+Alt+l", "KDE Plasma: lock (some versions)"),
    ("Alt+F1", "KDE Plasma: application launcher"),
    ("Ctrl+Alt+Escape", "KDE Plasma: kill a window"),
    ("Super+e", "KDE Plasma: file manager"),
    ("Super+p", "KDE Plasma / GNOME / Xfce: display switch"),
    # Xfce defaults
    ("Super+r", "Xfce: application finder"),
    ("Super+t", "Xfce: terminal"),
    ("Ctrl+Alt+l", "Xfce: lock screen"),
    ("Ctrl+Alt+Escape", "Xfce: kill a window"),
)

MAC_RESERVED: tuple[tuple[str, str], ...] = (
    ("Super+space", "Spotlight"),
    ("Super+Alt+space", "Finder search window"),
    ("Ctrl+space", "previous input source"),
    ("Ctrl+Alt+space", "next input source"),
    ("Ctrl+Up", "Mission Control"),
    ("Ctrl+Down", "application windows"),
    ("Ctrl+Left", "move a space left"),
    ("Ctrl+Right", "move a space right"),
    ("Super+Shift+3", "screenshot"),
    ("Super+Shift+4", "screenshot of a selection"),
    ("Super+Shift+5", "screenshot toolbar"),
    ("Ctrl+Super+Shift+3", "screenshot to the clipboard"),
    ("Ctrl+Super+Shift+4", "selection to the clipboard"),
    ("Super+Alt+F5", "accessibility shortcuts"),
    ("Super+F5", "VoiceOver on/off"),
    ("Ctrl+Super+q", "lock screen"),
    ("Ctrl+Super+f", "full screen"),
    ("Ctrl+Super+space", "emoji & symbols"),
    ("Ctrl+Alt+Super+8", "invert colours"),
    ("Super+Alt+8", "zoom on/off"),
    ("Super+Alt+equal", "zoom in"),
    ("Super+Alt+minus", "zoom out"),
    ("Super+Alt+d", "Dock hiding on/off"),
    ("Super+Alt+Escape", "force quit"),
    ("Super+Tab", "switch applications"),
    ("Super+grave", "next window of the app"),
    ("Super+h", "hide the app"),
    ("Super+Alt+h", "hide other apps"),
    ("Super+m", "minimise"),
    ("Super+q", "quit the app"),
    ("Super+w", "close the window"),
    ("Super+comma", "the app's settings"),
    ("Ctrl+F2", "focus the menu bar"),
    ("Ctrl+F3", "focus the Dock"),
    ("Ctrl+F7", "keyboard navigation"),
    ("Super+Shift+slash", "Help menu"),
)
# Not chords, so not in the table: Globe/fn+E (emoji), double-press Fn (dictation), and
# VoiceOver, which takes every ⌃⌥ chord as its VO key while VoiceOver is running.
