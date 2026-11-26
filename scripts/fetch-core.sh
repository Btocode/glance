#!/usr/bin/env bash
# Install glance_core (Harper in glanced's own process) into Glance's venv.
#
#   fetch-core.sh VENV_PYTHON [--with-rust]
#
# A prebuilt abi3 wheel is installed only when scripts/core-wheels.sha256 pins one for this
# platform, and only if its SHA-256 matches. --with-rust builds the crate from rust/ instead
# (needs cargo; takes minutes). With neither, nothing is installed and Glance keeps using
# harper-ls: users never need Rust. Exit status: 0 installed or nothing to do (fallback),
# 1 a pinned wheel failed its checksum or a build/install failed (Glance still works on
# harper-ls). bash 3.2-safe (macOS's /bin/bash).
#
# Environment (tests and unusual setups): GLANCE_CORE_PINS (pins file),
# GLANCE_CORE_BASE_URL (release download base), GLANCE_FETCH_PLATFORM ("Linux-x86_64", ...),
# GLANCE_UV (uv binary to install with; else uv on PATH; else the venv's pip).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SRC="$(dirname "$HERE")"
PINS="${GLANCE_CORE_PINS:-$HERE/core-wheels.sha256}"
BASE_URL="${GLANCE_CORE_BASE_URL:-https://github.com/Btocode/glance/releases/download}"

note() { printf '    %s\n' "$*"; }
warn() { printf 'fetch-core: %s\n' "$*" >&2; }

[ $# -ge 1 ] || { warn "usage: fetch-core.sh VENV_PYTHON [--with-rust]"; exit 2; }
PY="$1"
WITH_RUST=0
[ "${2:-}" = "--with-rust" ] && WITH_RUST=1
[ -x "$PY" ] || { warn "$PY is not an executable Python"; exit 1; }

pip_install() {
    local uv="${GLANCE_UV:-}"
    if [ -z "$uv" ] && command -v uv >/dev/null 2>&1; then uv="uv"; fi
    if [ -n "$uv" ]; then
        "$uv" pip install --quiet --python "$PY" --reinstall "$@" </dev/null
    else
        "$PY" -m pip install --quiet --force-reinstall "$@" </dev/null
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# The wheel-name suffix pattern for this machine, or nothing.
platform_pattern() {
    case "${GLANCE_FETCH_PLATFORM:-$(uname -s)-$(uname -m)}" in
        Linux-x86_64) echo 'manylinux.*_x86_64\.whl' ;;
        Linux-aarch64 | Linux-arm64) echo 'manylinux.*_aarch64\.whl' ;;
        Darwin-arm64 | Darwin-aarch64) echo 'macosx_.*_arm64\.whl' ;;
        Darwin-x86_64) echo 'macosx_.*_x86_64\.whl' ;;
        *) echo "" ;;
    esac
}

installed_ok() { "$PY" -c 'import glance_core' >/dev/null 2>&1; }

build_from_source() {
    if ! command -v cargo >/dev/null 2>&1; then
        warn "--with-rust needs cargo (https://rustup.rs); keeping harper-ls"
        return 1
    fi
    note "building glance_core from $SRC/rust/glance_core (cargo, a few minutes)"
    pip_install "$SRC/rust/glance_core" || { warn "the build failed; keeping harper-ls"; return 1; }
    installed_ok || { warn "glance_core built but does not import; keeping harper-ls"; return 1; }
    note "glance_core built and installed"
}

install_pinned() {
    local pattern="$1" line sha name version tmp
    line="$(grep -E "^[0-9a-f]{64}  glance_core-[^ ]*${pattern}\$" "$PINS" 2>/dev/null | tail -n 1)" \
        || line=""
    if [ -z "$line" ]; then
        return 2
    fi
    sha="${line%%  *}"
    name="${line#*  }"
    version="$(printf '%s' "$name" | cut -d- -f2)"
    if [ "$("$PY" -c 'import glance_core; print(glance_core.__version__)' 2>/dev/null)" \
        = "$version" ]; then
        note "glance_core $version already installed"
        return 0
    fi
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064  # expand now: $tmp is local
    trap "rm -rf '$tmp'" EXIT
    if ! curl -fsSL --retry 3 -o "$tmp/$name" "$BASE_URL/core-v$version/$name"; then
        warn "could not download $name; keeping harper-ls"
        return 1
    fi
    if [ "$(sha256_of "$tmp/$name")" != "$sha" ]; then
        warn "checksum mismatch for $name; not installed, keeping harper-ls"
        return 1
    fi
    pip_install "$tmp/$name" || { warn "could not install $name; keeping harper-ls"; return 1; }
    installed_ok || { warn "$name installed but does not import; keeping harper-ls"; return 1; }
    note "glance_core $version installed (pinned wheel, checksum matches)"
}

main() {
    if [ "$WITH_RUST" = 1 ]; then
        build_from_source
        return
    fi
    local pattern rc=0
    pattern="$(platform_pattern)"
    if [ -z "$pattern" ]; then
        note "no glance_core wheel for this platform; grammar runs on harper-ls"
        return 0
    fi
    install_pinned "$pattern" || rc=$?
    if [ "$rc" = 2 ]; then
        note "no pinned glance_core wheel yet; grammar runs on harper-ls"
        return 0
    fi
    return "$rc"
}

main
