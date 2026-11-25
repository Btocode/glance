#!/usr/bin/env bash
# Glance uninstaller: reverses install.sh, using the record it left in
# ~/.local/share/glance/install-manifest.json.
#
#   ./uninstall.sh [--keep-data]
#
# Every step is safe when its target is already gone, so it can be run twice or after an
# install that stopped half-way. It only removes what Glance put there: symlinks that point
# into Glance's own venv, its unit, its block in .zshrc, its data. Your settings in
# ~/.config/glance/config.toml are kept.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: uninstall.sh [--keep-data]

  --keep-data   keep ~/.local/share/glance (personal dictionary, ignored rules); only the
                grammar engine binary and the install record are removed from it
  -h, --help    show this help
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
    SYS_PY="${GLANCE_SYSTEM_PYTHON:-/usr/bin/python3}"
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/glance"
    MANIFEST="$DATA_DIR/install-manifest.json"
    OVERRIDES="$DATA_DIR/desktop-overrides.json"
    APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    BIN_DIR="$HOME/.local/bin"
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/glance/config.toml"
    UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/glance.service"
    RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/glance"
    BIN_NAMES=(glance glanced glance-capture glance-edit)
    HERE="$(script_dir)" || HERE=""
}

# See install.sh: a scalar (booleans as 1/0) or one list item per line.
manifest_get() {
    if [ ! -f "$MANIFEST" ] || [ ! -x "$SYS_PY" ]; then return 0; fi
    "$SYS_PY" -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        v = json.load(f).get(sys.argv[2])
except (OSError, ValueError, AttributeError):
    sys.exit(0)
if isinstance(v, bool):
    print(int(v))
elif isinstance(v, list):
    for x in v:
        print(x["path"] + "\t" + x["target"] if isinstance(x, dict) else x)
elif v is not None:
    print(v)
' "$MANIFEST" "$1" || true
}

# What the installer recorded; without a record, what it would have done from this checkout.
load_manifest() {
    SRC="$(manifest_get src_dir)"
    CLONED="$(manifest_get cloned)"
    VENV="$(manifest_get venv)"
    VENV_CREATED="$(manifest_get venv_created)"
    ZSHRC="$(manifest_get zshrc)"
    ZSHRC_EXISTED="$(manifest_get zshrc_existed)"
    mapfile -t LINKS < <(manifest_get symlinks)
    mapfile -t CREATED_DIRS < <(manifest_get created_dirs)
    if [ -z "$SRC" ] && [ -n "$HERE" ]; then SRC="$HERE"; fi
    if [ -z "$VENV" ] && [ -n "$HERE" ]; then VENV="$HERE/.venv"; fi
    if [ -z "$ZSHRC" ]; then ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"; fi
    if [ -n "$(manifest_get unit)" ]; then UNIT="$(manifest_get unit)"; fi
}

user_systemctl() {
    [ -n "${GLANCE_SKIP_SYSTEMCTL:-}" ] && return 0
    systemctl --user "$@" </dev/null >/dev/null 2>&1 || true
}

stop_service() {
    say "Stopping Glance"
    user_systemctl disable --now glance.service
}

# The launcher overrides and the claude() block in .zshrc: doctor put them there and knows
# how to take them out byte for byte.
# Prints, one per line, what doctor --fix put in place and is still there: the overrides
# record, launcher files carrying Glance's marker, backups of launchers it edited, and the
# claude() block. Empty output means there is nothing left to undo.
doctor_leftovers() {
    if [ -e "$OVERRIDES" ]; then say "$OVERRIDES"; fi
    local f
    for f in "$APPS_DIR"/*.desktop "$APPS_DIR"/*.glance-bak; do
        [ -f "$f" ] || continue
        case "$f" in
            *.glance-bak) say "$f (a launcher backup)" ;;
            *) if grep -qxF 'X-Glance-Override=true' "$f" 2>/dev/null; then say "$f"; fi ;;
        esac
    done
    if grep -qF '>>> glance claude wrapper >>>' "$ZSHRC" 2>/dev/null; then
        say "$ZSHRC (the block between the 'glance claude wrapper' markers)"
    fi
}

# Runs `glance doctor --undo` with whatever still works: the venv's command, the venv's
# python, then the system python on the source tree. Success is judged by what is left on
# disk, not by the exit code (doctor --undo reports the checks afterwards, and those fail
# once Glance is being removed).
undo_doctor() {
    say "Undoing what glance doctor set up"
    UNDO_FAILED=0
    if [ -z "$(doctor_leftovers)" ]; then return 0; fi
    local -a tried=()
    if [ -n "$VENV" ] && [ -x "$VENV/bin/glance" ]; then
        "$VENV/bin/glance" doctor --undo </dev/null >/dev/null 2>&1 || true
        tried+=("$VENV/bin/glance")
    fi
    if [ -n "$(doctor_leftovers)" ] && [ -n "$VENV" ] && [ -x "$VENV/bin/python" ]; then
        "$VENV/bin/python" -m glance.cli doctor --undo </dev/null >/dev/null 2>&1 || true
        tried+=("$VENV/bin/python")
    fi
    if [ -n "$(doctor_leftovers)" ] && [ -n "$SRC" ] && [ -d "$SRC/src/glance" ] \
        && [ -x "$SYS_PY" ]; then
        PYTHONPATH="$SRC/src" "$SYS_PY" -m glance.cli doctor --undo </dev/null >/dev/null 2>&1 \
            || true
        tried+=("$SYS_PY")
    fi
    if [ -n "$(doctor_leftovers)" ]; then
        UNDO_FAILED=1
        if [ ${#tried[@]} -eq 0 ]; then
            note "Glance's own environment is gone, so the undo could not run"
        else
            note "glance doctor --undo did not finish (tried: ${tried[*]})"
        fi
    fi
    # A .zshrc that exists only because the installer added the wrapper to it.
    if [ "$ZSHRC_EXISTED" = 0 ] && [ -f "$ZSHRC" ] && [ ! -s "$ZSHRC" ]; then
        rm -f "$ZSHRC"
    fi
}

# After a failed undo: say what is still there and how to finish.
report_leftovers() {
    say ""
    say "Glance is NOT fully uninstalled. These are still in place:"
    local line
    while IFS= read -r line; do note "$line"; done < <(doctor_leftovers)
    say ""
    say "Kept so this can be finished: $OVERRIDES, the install record and $SRC."
    say "Re-run the installer (it repairs Glance's environment), then run this script again."
    say "Or by hand: delete the files above that contain X-Glance-Override=true, rename each"
    say "*.glance-bak back to its name without that suffix, delete the marked block in"
    say "$ZSHRC, then run this script again."
}

remove_unit() {
    if [ -e "$UNIT" ] || [ -L "$UNIT" ]; then
        rm -f "$UNIT"
        note "removed $UNIT"
    fi
    user_systemctl daemon-reload
}

# Removes $BIN_DIR/NAME only when it is a symlink the installer recorded, or one into the
# venv being removed. Anything else there is somebody else's.
remove_links() {
    local name link now line ours
    for name in "${BIN_NAMES[@]}"; do
        link="$BIN_DIR/$name"
        [ -L "$link" ] || continue
        now="$(readlink "$link")"
        ours=0
        if [ -n "$VENV" ] && [ "$now" = "$VENV/bin/$name" ]; then ours=1; fi
        for line in "${LINKS[@]}"; do
            if [ "$line" = "$link"$'\t'"$now" ]; then ours=1; fi
        done
        if [ "$ours" = 1 ]; then
            rm -f "$link"
            note "removed $link"
        else
            note "left $link (it points to $now, not to Glance)"
        fi
    done
}

remove_data() {
    rm -rf "$RUNTIME"
    [ -d "$DATA_DIR" ] || return 0
    if [ "$UNDO_FAILED" = 1 ]; then
        # The overrides record is what a later undo works from, and the install record says
        # what else to remove then; everything else goes as usual.
        local entry
        for entry in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
            [ -e "$entry" ] || continue
            case "$entry" in
                "$OVERRIDES" | "$MANIFEST") ;;
                "$DATA_DIR/bin") rm -rf "$entry" ;;
                *) if [ "$KEEP_DATA" = 0 ]; then rm -rf "$entry"; fi ;;
            esac
        done
        return 0
    fi
    if [ "$KEEP_DATA" = 1 ]; then
        rm -f "$MANIFEST" "$DATA_DIR/bin/harper-ls"
        rmdir "$DATA_DIR/bin" 2>/dev/null || true
        say "Kept your data in $DATA_DIR"
        return 0
    fi
    say "Deleting $DATA_DIR:"
    local entry
    for entry in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
        if [ -e "$entry" ]; then note "$(basename "$entry")"; fi
    done
    rm -rf "$DATA_DIR"
}

# The venv and the source are removed only when the installer created them.
remove_source() {
    cd /
    [ "$UNDO_FAILED" = 0 ] || return 0  # the undo needs Glance's code next time
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
    local i
    for ((i = ${#CREATED_DIRS[@]} - 1; i >= 0; i--)); do
        rmdir "${CREATED_DIRS[i]}" 2>/dev/null || true
    done
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
    stop_service
    undo_doctor
    remove_unit
    remove_links
    remove_data
    remove_source
    remove_created_dirs
    if [ "$UNDO_FAILED" = 1 ]; then
        report_leftovers
        exit 1
    fi
    say "Glance is uninstalled."
    if [ -f "$CONFIG" ]; then
        say "Your settings were kept: $CONFIG"
    fi
}

{ main "$@"; exit $?; }
