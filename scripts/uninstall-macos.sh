#!/bin/bash
# Remove Glance from a Mac: exactly what scripts/install-macos.sh recorded in
# ~/.local/share/glance/install-manifest.macos. uninstall.sh hands off here on macOS.
# Your settings (~/.config/glance) are kept. bash 3.2-safe.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: uninstall.sh [--keep-data]      (on macOS)

Stops Glance, removes its LaunchAgent, commands, environment, grammar engine and logs, and
the source when the installer downloaded it. --keep-data keeps your dictionary and usage data
in ~/.local/share/glance. Your settings (~/.config/glance) are always kept. The Accessibility
permission is yours to remove in System Settings (Glance cannot change it).
EOF
}

say() { printf '%s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'uninstall.sh: %s\n' "$*" >&2; exit 1; }

script_dir() {
    local src="${BASH_SOURCE[0]:-}"
    if [ -z "$src" ] || [ ! -f "$src" ]; then return 1; fi
    (cd "$(dirname "$src")" && pwd -P)
}

setup_paths() {
    if [ -n "${GLANCE_HOME:-}" ]; then
        HOME="$GLANCE_HOME"
        export HOME
        unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME ZDOTDIR
    fi
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/glance"
    STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/glance"
    MANIFEST="$DATA_DIR/install-manifest.macos"
    BIN_DIR="$HOME/.local/bin"
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/glance/config.toml"
    LABEL="io.github.btocode.glance"
    AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
    LOG_DIR="$HOME/Library/Logs/Glance"
    BIN_NAMES="glance glanced glance-capture glance-edit"
    HERE="$(script_dir)" || HERE=""
    HERE="${HERE%/scripts}"
}

manifest_get() {
    [ -f "$MANIFEST" ] || return 0
    local k v
    while IFS=$'\t' read -r k v; do
        if [ "$k" = "$1" ]; then printf '%s\n' "$v"; fi
    done <"$MANIFEST"
}

load_manifest() {
    SRC="$(manifest_get src_dir | tail -n 1)"
    CLONED="$(manifest_get cloned | tail -n 1)"
    VENV="$(manifest_get venv | tail -n 1)"
    VENV_CREATED="$(manifest_get venv_created | tail -n 1)"
    LOGS_CREATED="$(manifest_get logs_created | tail -n 1)"
    LINKS="$(manifest_get link)"
    CREATED_DIRS="$(manifest_get dir)"
    local a
    a="$(manifest_get agent | tail -n 1)"
    if [ -n "$a" ]; then AGENT="$a"; fi
    if [ -z "$SRC" ] && [ -n "$HERE" ]; then SRC="$HERE"; fi
    if [ -z "$VENV" ] && [ -n "$HERE" ]; then VENV="$HERE/.venv"; fi
    : "${CLONED:=0}" "${VENV_CREATED:=0}" "${LOGS_CREATED:=0}"
}

launchctl_do() {
    [ -n "${GLANCE_SKIP_LAUNCHCTL:-}" ] && return 0
    launchctl "$@" </dev/null >/dev/null 2>&1 || true
}

stop_agent() {
    say "Stopping Glance"
    launchctl_do bootout "gui/$(id -u)/$LABEL"
    if [ -e "$AGENT" ] || [ -L "$AGENT" ]; then
        rm -f "$AGENT"
        note "removed $AGENT"
    fi
}

remove_links() {
    local name link now line ours
    for name in $BIN_NAMES; do
        link="$BIN_DIR/$name"
        [ -L "$link" ] || continue
        now="$(readlink "$link")"
        ours=0
        if [ -n "$VENV" ] && [ "$now" = "$VENV/bin/$name" ]; then ours=1; fi
        while IFS= read -r line; do
            if [ "$line" = "$link"$'\t'"$now" ]; then ours=1; fi
        done <<EOF
$LINKS
EOF
        if [ "$ours" = 1 ]; then
            rm -f "$link"
            note "removed $link"
        else
            note "left $link (it points to $now, not to Glance)"
        fi
    done
}

remove_data() {
    rm -rf "$STATE_DIR/run"
    if [ "$KEEP_DATA" = 0 ]; then rm -rf "$STATE_DIR"; fi  # update.log and the sockets
    [ -d "$DATA_DIR" ] || return 0
    if [ "$KEEP_DATA" = 1 ]; then
        rm -rf "${DATA_DIR:?}/bin" "$DATA_DIR/python" "$DATA_DIR/uv" "$DATA_DIR/uv-cache"
        rm -f "$MANIFEST"
        say "Kept your data in $DATA_DIR"
        return 0
    fi
    say "Deleting $DATA_DIR"
    rm -rf "$DATA_DIR"
}

remove_logs() {
    if [ "$LOGS_CREATED" = 1 ] && [ -d "$LOG_DIR" ]; then
        rm -rf "$LOG_DIR"
        note "removed $LOG_DIR"
    fi
}

remove_source() {
    cd /
    if [ "$CLONED" = 1 ] && [ -n "$SRC" ] && [ "$SRC" != "$HOME" ] && [ -f "$SRC/pyproject.toml" ]; then
        rm -rf "$SRC"
        note "removed $SRC"
    elif [ "$VENV_CREATED" = 1 ] && [ -f "$VENV/pyvenv.cfg" ]; then
        rm -rf "$VENV"
        note "removed $VENV"
    fi
}

# Directories the installer had to create, deepest first; rmdir leaves any that are in use.
remove_created_dirs() {
    local d
    while IFS= read -r d; do
        if [ -n "$d" ]; then rmdir "$d" 2>/dev/null || true; fi
    done <<EOF
$(printf '%s\n' "$CREATED_DIRS" | awk 'NF {a[n++] = $0} END {while (n) print a[--n]}')
EOF
}

main() {
    KEEP_DATA=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --keep-data) KEEP_DATA=1 ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
    setup_paths
    load_manifest
    stop_agent
    remove_links
    remove_data
    remove_logs
    remove_source
    remove_created_dirs
    say "Glance is uninstalled."
    if [ -f "$CONFIG" ]; then
        say "Your settings were kept: $CONFIG"
    fi
    say "To remove its Accessibility permission: System Settings → Privacy & Security →"
    say "Accessibility, select the Python entry Glance used, and click −."
}

{ main "$@"; exit $?; }
