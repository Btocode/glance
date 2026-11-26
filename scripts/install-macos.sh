#!/bin/bash
# Glance installer for macOS (13 or later, Apple silicon or Intel).
#
#   curl -fsSL https://raw.githubusercontent.com/Btocode/glance/main/install.sh | bash
#
# install.sh hands off to this script on a Mac. It needs no sudo and no Homebrew and changes
# no shell rc file: uv (installed under ~/.local/share/glance/uv when it is not on PATH)
# brings its own CPython 3.12 into ~/.local/share/glance/python, the venv sits in the source
# tree, and a LaunchAgent starts glanced at login. Everything it creates is recorded in
# ~/.local/share/glance/install-manifest.macos, and scripts/uninstall-macos.sh reverses
# exactly that. bash 3.2 (macOS's /bin/bash): no mapfile, no associative arrays.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [options]          (on macOS)

  --from-local            install from the checkout this script sits in
  --no-start              install the LaunchAgent but do not start Glance now
  --with-rust             build the in-process grammar engine (glance_core) from source with
                          cargo instead of using a pinned prebuilt wheel
  -h, --help              show this help

Environment (for tests and unusual setups): GLANCE_HOME (use instead of $HOME), GLANCE_SRC,
GLANCE_REPO_URL, GLANCE_TARBALL_URL, GLANCE_UV, GLANCE_PYTHON_VERSION, GLANCE_NO_GIT,
GLANCE_SKIP_CLONE, GLANCE_SKIP_VENV, GLANCE_SKIP_FETCH, GLANCE_SKIP_LAUNCHCTL,
GLANCE_SKIP_DOCTOR, GLANCE_SKIP_SWVERS.
EOF
}

say() { printf '%s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
# Apple's /usr/bin/git without the command line tools opens an install dialog: only use git
# when the tools are really there.
have_git() {
    [ -z "${GLANCE_NO_GIT:-}" ] || return 1
    have git || return 1
    if [ "$(uname -s)" = Darwin ] && [ -z "${GLANCE_SKIP_SWVERS:-}" ]; then
        xcode-select -p >/dev/null 2>&1 || return 1
    fi
    return 0
}

STEP=0
step() {
    STEP=$((STEP + 1))
    printf '\n==> [%d/8] %s\n' "$STEP" "$*"
}

# ---------------------------------------------------------------------- options and paths

parse_args() {
    FROM_LOCAL=0 NO_START=0 WITH_RUST=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --from-local) FROM_LOCAL=1 ;;
            --no-start) NO_START=1 ;;
            --with-rust) WITH_RUST=1 ;;
            # Linux-only options are accepted and ignored, so one command works everywhere.
            --with-apt | --yes | -y | --no-wrapper | --no-desktop-overrides) ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
}

script_dir() {
    local src="${BASH_SOURCE[0]:-}"
    if [ -z "$src" ] || [ ! -f "$src" ]; then return 1; fi
    (cd "$(dirname "$src")" && pwd -P)
}

is_checkout() {
    [ -f "$1/pyproject.toml" ] && [ -f "$1/scripts/install-macos.sh" ] \
        && [ -f "$1/scripts/fetch-harper.sh" ]
}

setup_paths() {
    if [ -n "${GLANCE_HOME:-}" ]; then
        HOME="$GLANCE_HOME"
        export HOME
        unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME ZDOTDIR
    fi
    [ -d "$HOME" ] || die "home directory $HOME does not exist"
    DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}"
    DATA_DIR="$DATA_ROOT/glance"
    MANIFEST="$DATA_DIR/install-manifest.macos"
    STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/glance"
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/glance"
    BIN_DIR="$HOME/.local/bin"
    AGENT="$HOME/Library/LaunchAgents/io.github.btocode.glance.plist"
    LABEL="io.github.btocode.glance"
    LOG_DIR="$HOME/Library/Logs/Glance"
    PY_DIR="$DATA_DIR/python"
    PY_VERSION="${GLANCE_PYTHON_VERSION:-3.12}"
    BIN_NAMES="glance glanced glance-capture glance-edit"

    local here=""
    here="$(script_dir)" || here=""
    here="${here%/scripts}"
    if [ -n "$here" ] && is_checkout "$here" && [ -z "${GLANCE_SRC:-}" ]; then
        FROM_LOCAL=1
        SRC="$here"
    elif [ "$FROM_LOCAL" = 1 ]; then
        die "--from-local needs a Glance checkout: run ./install.sh inside one"
    else
        SRC="${GLANCE_SRC:-$DATA_ROOT/glance-src}"
    fi
    VENV="$SRC/.venv"
}

# ---------------------------------------------------------------------- manifest

# One "key<TAB>value" per line; `link` and `dir` repeat. Plain text: bash 3.2 reads it with
# nothing but `read`, and so does uninstall-macos.sh.
manifest_get() {
    [ -f "$MANIFEST" ] || return 0
    local k v
    while IFS=$'\t' read -r k v; do
        if [ "$k" = "$1" ]; then printf '%s\n' "$v"; fi
    done <"$MANIFEST"
}

load_previous() {
    CLONED="$(manifest_get cloned | tail -n 1)"
    VENV_CREATED="$(manifest_get venv_created | tail -n 1)"
    UV_INSTALLED="$(manifest_get uv_installed | tail -n 1)"
    CREATED_DIRS="$(manifest_get dir)"
    PREV_LINKS="$(manifest_get link)"
    HAD_MANIFEST=0
    [ -f "$MANIFEST" ] && HAD_MANIFEST=1
    : "${CLONED:=0}" "${VENV_CREATED:=0}" "${UV_INSTALLED:=0}"
    if [ "$(manifest_get src_dir | tail -n 1)" != "$SRC" ]; then
        CLONED=0
        VENV_CREATED=0
    fi
    LINKS=""
    AGENT_WRITTEN=""
    LOGS_CREATED="$(manifest_get logs_created | tail -n 1)"
    : "${LOGS_CREATED:=0}"
}

write_manifest() {
    local tmp="$MANIFEST.tmp" line
    {
        printf 'version\t1\n'
        printf 'platform\tmacos\n'
        printf 'src_dir\t%s\n' "$SRC"
        printf 'cloned\t%s\n' "$CLONED"
        printf 'venv\t%s\n' "$VENV"
        printf 'venv_created\t%s\n' "$VENV_CREATED"
        printf 'uv_installed\t%s\n' "$UV_INSTALLED"
        printf 'python_dir\t%s\n' "$PY_DIR"
        printf 'agent\t%s\n' "$AGENT_WRITTEN"
        printf 'logs_created\t%s\n' "$LOGS_CREATED"
        while IFS= read -r line; do
            [ -n "$line" ] && printf 'link\t%s\n' "$line"
        done <<EOF
$LINKS
EOF
        while IFS= read -r line; do
            [ -n "$line" ] && printf 'dir\t%s\n' "$line"
        done <<EOF
$CREATED_DIRS
EOF
    } >"$tmp"
    mv -f "$tmp" "$MANIFEST"
}

# mkdir -p that remembers each directory it had to create (outermost first).
ensure_dir() {
    local d="$1" missing=""
    while [ ! -d "$d" ]; do
        missing="$d
$missing"
        d="$(dirname "$d")"
    done
    mkdir -p "$1"
    local m
    while IFS= read -r m; do
        [ -n "$m" ] && CREATED_DIRS="$CREATED_DIRS
$m"
    done <<EOF
$missing
EOF
    CREATED_DIRS="$(printf '%s\n' "$CREATED_DIRS" | sed '/^$/d')"
}

# ---------------------------------------------------------------------- step 1

check_prerequisites() {
    step "Checking the system"
    if [ -z "${GLANCE_SKIP_SWVERS:-}" ]; then
        local ver major
        ver="$(sw_vers -productVersion 2>/dev/null || echo 0)"
        major="${ver%%.*}"
        [ "$major" -ge 13 ] 2>/dev/null || die "Glance needs macOS 13 or later; this is $ver."
        note "macOS $ver, $(uname -m)"
    fi
    case "$(uname -m)" in
        arm64 | x86_64) ;;
        *) die "unsupported architecture $(uname -m)" ;;
    esac
    have curl || die "curl is needed."
    case "$HOME$SRC" in
        *[[:space:]%]*) die "a path with a space or '%' cannot be used: $HOME / $SRC" ;;
    esac
}

# ---------------------------------------------------------------------- step 2: source

fetch_tarball() {
    local url="${GLANCE_TARBALL_URL:-https://github.com/Btocode/glance/archive/refs/heads/main.tar.gz}"
    note "downloading $url"
    ensure_dir "$(dirname "$SRC")"
    local new
    new="$(mktemp -d "$SRC.new.XXXXXX")"
    if ! curl -fsSL --retry 3 "$url" </dev/null | tar -xz --strip-components=1 -C "$new" \
        || ! is_checkout "$new"; then
        rm -rf "$new"
        die "could not download Glance from $url"
    fi
    chmod 755 "$new"
    if [ -e "$SRC" ]; then
        if [ -d "$SRC/.venv" ]; then rm -rf "$new/.venv"; mv "$SRC/.venv" "$new/.venv"; fi
        rm -rf "$SRC"
    fi
    mv "$new" "$SRC"
}

get_source() {
    step "Getting Glance"
    if [ "$FROM_LOCAL" = 1 ]; then
        note "using the checkout at $SRC"
        return
    fi
    if [ -n "${GLANCE_SKIP_CLONE:-}" ]; then
        is_checkout "$SRC" || die "GLANCE_SKIP_CLONE is set but $SRC is not a Glance checkout."
        note "using $SRC as it is"
        return
    fi
    local url="${GLANCE_REPO_URL:-https://github.com/Btocode/glance.git}" fresh=0
    if [ ! -e "$SRC" ] || [ -z "$(ls -A "$SRC")" ]; then
        fresh=1
    elif ! is_checkout "$SRC"; then
        die "$SRC exists and is not a Glance checkout. Move it away, or set GLANCE_SRC."
    fi
    if [ -d "$SRC/.git" ] && have_git; then
        note "updating $SRC"
        GIT_TERMINAL_PROMPT=0 git -C "$SRC" pull --ff-only --quiet </dev/null \
            || die "could not fast-forward $SRC. Resolve it there (git status), then run again."
    elif [ "$fresh" = 1 ] && have_git; then
        note "cloning $url into $SRC"
        ensure_dir "$(dirname "$SRC")"
        GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 "$url" "$SRC" </dev/null \
            || die "could not clone $url"
    else
        fetch_tarball
    fi
    if [ "$fresh" = 1 ]; then CLONED=1; fi
    is_checkout "$SRC" || die "$SRC does not look like Glance after the download."
}

# ---------------------------------------------------------------------- step 3: uv, python, venv

find_uv() {
    if [ -n "${GLANCE_UV:-}" ]; then UV="$GLANCE_UV"; return; fi
    if [ -x "$DATA_DIR/uv/uv" ]; then UV="$DATA_DIR/uv/uv"; return; fi
    if have uv; then UV="$(command -v uv)"; return; fi
    note "uv is not installed: installing it into $DATA_DIR/uv (no shell files are changed)"
    ensure_dir "$DATA_DIR/uv"
    UV_INSTALLED=1
    write_manifest
    # UV_UNMANAGED_INSTALL: into that directory only - no receipt in ~/.config/uv, no PATH
    # or shell-file changes.
    curl -LsSf https://astral.sh/uv/install.sh </dev/null \
        | env UV_UNMANAGED_INSTALL="$DATA_DIR/uv" sh >/dev/null || die "could not install uv"
    UV="$DATA_DIR/uv/uv"
    [ -x "$UV" ] || die "uv did not install into $DATA_DIR/uv"
}

# uv's own Python and its cache, under Glance's data dir: nothing goes into ~/.local/bin or
# ~/.cache, so the uninstaller can take back everything.
uv_env() {
    env UV_PYTHON_INSTALL_DIR="$PY_DIR" UV_PYTHON_BIN_DIR="$PY_DIR/bin" UV_PYTHON_INSTALL_BIN=0 \
        UV_CACHE_DIR="$DATA_DIR/uv-cache" UV_PYTHON_PREFERENCE=only-managed "$@"
}

make_venv() {
    step "Building the Python environment (uv, CPython $PY_VERSION, PyObjC)"
    if [ -n "${GLANCE_SKIP_VENV:-}" ]; then
        note "skipped (GLANCE_SKIP_VENV)"
    else
        find_uv
        ensure_dir "$PY_DIR"
        write_manifest
        uv_env "$UV" python install --quiet "$PY_VERSION" </dev/null \
            || die "could not install Python $PY_VERSION with uv"
        if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import sys
sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
            note "$VENV exists, kept"
        else
            rm -rf "$VENV"
            uv_env "$UV" venv --quiet --python "$PY_VERSION" "$VENV" </dev/null \
                || die "could not create $VENV"
            VENV_CREATED=1
        fi
        write_manifest
        uv_env "$UV" pip install --quiet --python "$VENV/bin/python" -e "$SRC[macos]" </dev/null \
            || die "could not install Glance into $VENV"
    fi
    [ -x "$VENV/bin/glance" ] || die "$VENV/bin/glance is missing: the install did not complete."
}

# ---------------------------------------------------------------------- step 4: harper

fetch_harper() {
    step "Fetching the grammar engine (harper-ls, SHA-256 pinned)"
    if [ -n "${GLANCE_SKIP_FETCH:-}" ]; then
        note "skipped (GLANCE_SKIP_FETCH)"
        return
    fi
    local bin="$DATA_DIR/bin/harper-ls" key want
    case "$(uname -m)" in
        arm64) key=DARWIN_ARM64_BINARY_SHA256 ;;
        *) key=DARWIN_X86_64_BINARY_SHA256 ;;
    esac
    want="$(sed -n "s/^$key=\"\([0-9a-f]\{64\}\)\"\$/\1/p" "$SRC/scripts/fetch-harper.sh")"
    if [ -n "$want" ] && [ -x "$bin" ] \
        && [ "$(shasum -a 256 "$bin" | cut -d' ' -f1)" = "$want" ]; then
        note "already installed, checksum matches"
        return
    fi
    XDG_DATA_HOME="$DATA_ROOT" bash "$SRC/scripts/fetch-harper.sh" </dev/null
}

# The in-process engine: a pinned wheel when one exists for this Mac (or a source build with
# --with-rust). Never fatal: without it glanced runs harper-ls, fetched above.
install_core() {
    if [ -n "${GLANCE_SKIP_FETCH:-}" ] || [ -n "${GLANCE_SKIP_VENV:-}" ]; then
        return
    fi
    [ -f "$SRC/scripts/fetch-core.sh" ] || return 0
    local rust=""
    [ "$WITH_RUST" = 1 ] && rust="--with-rust"
    # shellcheck disable=SC2086  # $rust is one flag or nothing
    uv_env GLANCE_UV="$UV" bash "$SRC/scripts/fetch-core.sh" "$VENV/bin/python" $rust </dev/null \
        || note "the in-process grammar engine is not installed; grammar runs on harper-ls"
}

# ---------------------------------------------------------------------- step 5: symlinks

is_our_link() {
    local now line
    now="$(readlink "$1")"
    [ "$now" = "$2" ] && return 0
    while IFS= read -r line; do
        [ "$line" = "$1"$'\t'"$now" ] && return 0
    done <<EOF
$PREV_LINKS
EOF
    return 1
}

refuse_foreign_binaries() {
    local name link
    for name in $BIN_NAMES; do
        link="$BIN_DIR/$name"
        if [ -L "$link" ]; then
            is_our_link "$link" "$VENV/bin/$name" || die "$link is a symlink to" \
                "$(readlink "$link"), not to Glance. Move it away and run the installer again."
        elif [ -e "$link" ]; then
            die "$link already exists and is not Glance's. Move it away and run the installer again."
        fi
    done
}

install_links() {
    step "Linking the commands into $BIN_DIR"
    refuse_foreign_binaries
    ensure_dir "$BIN_DIR"
    local name
    LINKS=""
    for name in $BIN_NAMES; do
        if [ -x "$VENV/bin/$name" ]; then
            ln -sfn "$VENV/bin/$name" "$BIN_DIR/$name"
            LINKS="$LINKS$BIN_DIR/$name"$'\t'"$VENV/bin/$name
"
            note "$name"
        fi
    done
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) note "note: $BIN_DIR is not on your PATH; run $BIN_DIR/glance, or add it to PATH" ;;
    esac
    write_manifest
}

# ---------------------------------------------------------------------- step 6: launchd

launchctl_do() {
    if [ -n "${GLANCE_SKIP_LAUNCHCTL:-}" ]; then
        note "skipped (GLANCE_SKIP_LAUNCHCTL): launchctl $*"
        return 0
    fi
    launchctl "$@" </dev/null
}

install_agent() {
    step "Installing the LaunchAgent ($LABEL)"
    ensure_dir "$(dirname "$AGENT")"
    if [ ! -d "$LOG_DIR" ]; then
        ensure_dir "$LOG_DIR"
        LOGS_CREATED=1
    fi
    "$VENV/bin/python" -c '
import sys
from pathlib import Path
from glance.platform.macos.launchd import default_path_env, render_plist
home = Path(sys.argv[2])
sys.stdout.buffer.write(render_plist(Path(sys.argv[1]), home, default_path_env(home)))
' "$VENV/bin/glanced" "$HOME" >"$AGENT.tmp" </dev/null || die "could not write $AGENT"
    mv -f "$AGENT.tmp" "$AGENT"
    AGENT_WRITTEN="$AGENT"
    write_manifest
    note "$AGENT"
    local uid
    uid="$(id -u)"
    launchctl_do bootout "gui/$uid/$LABEL" 2>/dev/null || true
    if [ "$NO_START" = 1 ]; then
        note "installed; it starts at your next login (--no-start)"
    else
        launchctl_do bootstrap "gui/$uid" "$AGENT" || die "launchctl could not load $AGENT"
        [ -n "${GLANCE_SKIP_LAUNCHCTL:-}" ] || note "started (logs: $LOG_DIR/glanced.log)"
    fi
}

# ---------------------------------------------------------------------- steps 7-8: doctor

run_doctor() {
    step "Checking the setup (glance doctor)"
    if [ -n "${GLANCE_SKIP_DOCTOR:-}" ]; then
        note "skipped (GLANCE_SKIP_DOCTOR)"
        return
    fi
    sleep "${GLANCE_DOCTOR_WAIT:-2}"
    "$VENV/bin/glance" doctor </dev/null || true
}

print_report() {
    step "Result"
    local py
    py="$(cd "$VENV/bin" && pwd -P)/python"
    py="$("$py" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null \
        || echo "$VENV/bin/python")"
    say "Glance is installed."
    say ""
    say "One more step, once: allow Glance to read and write text fields."
    say "    System Settings → Privacy & Security → Accessibility → +"
    say "    add: $py"
    say "    (press ⌘⇧G in the file dialog and paste that path), switch it on, then run:"
    say "    launchctl kickstart -k gui/$(id -u)/$LABEL"
    say ""
    say "    glance doctor    check the setup again at any time"
    say "    glance status    what Glance is doing right now"
    say "    uninstall        $SRC/uninstall.sh"
}

main() {
    parse_args "$@"
    setup_paths
    check_prerequisites            # 1
    load_previous
    refuse_foreign_binaries
    get_source                     # 2
    ensure_dir "$DATA_DIR"
    # Glance makes these at run time; made (and so recorded) here, the uninstaller can take
    # back the ones that were not there before. Settings in ~/.config/glance stay.
    ensure_dir "$STATE_DIR"
    ensure_dir "$CONFIG_DIR"
    write_manifest
    make_venv                      # 3
    fetch_harper                   # 4
    install_core                   # 4
    install_links                  # 5
    install_agent                  # 6
    run_doctor                     # 7
    print_report                   # 8
}

{ main "$@"; exit $?; }
