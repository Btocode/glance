#!/bin/bash
# Run a command inside a PRIVATE session for the Wayland tests: its own HOME, XDG dirs,
# dconf, runtime dir and session bus (dbus-run-session). The headless GNOME Shell the tests
# start lives only in there; the user's real session, settings and clipboard are never used.
#
#   tests/wayland/private-session.sh ROOT CMD...
#
# ROOT is a scratch directory (created; e.g. ~/glance-wltest). Nothing outside it is written.
set -eu
ROOT=$(realpath -m "$1"); shift
export HOME="$ROOT/home"
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache" XDG_STATE_HOME="$HOME/.local/state"
export XDG_RUNTIME_DIR="$ROOT/run"
mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME" "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
unset DISPLAY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS AT_SPI_BUS_ADDRESS SESSION_MANAGER \
      GNOME_KEYRING_CONTROL SSH_AUTH_SOCK GNOME_SETUP_DISPLAY XAUTHORITY XDG_SESSION_ID
export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_DESKTOP=gnome
export GLANCE_WAYLAND_PRIVATE="$ROOT"
exec dbus-run-session -- "$@"
