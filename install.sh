#!/usr/bin/env bash
# Glance installer (Linux, X11).
#
#   curl -fsSL https://raw.githubusercontent.com/Btocode/glance/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/Btocode/glance/main/install.sh | bash -s -- --with-apt
#   ./install.sh --from-local        from a checkout
#
# Safe to run again: it updates the source and repairs whatever is missing. Everything it
# creates is recorded in ~/.local/share/glance/install-manifest.json, and uninstall.sh
# reverses exactly that. It never runs sudo unless --with-apt is given, and never changes
# $EDITOR. The whole script is functions plus one call on the last line, so a download that
# is cut short runs nothing.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: install.sh [options]

  --from-local            install from the checkout this script sits in (the default when it
                          sits in one); otherwise the source is cloned to
                          ~/.local/share/glance-src and updated with `git pull --ff-only`
  --with-apt              install missing system packages with `sudo apt-get install -y`
                          (without it the command is printed and nothing runs as root)
  --yes                   answer yes to the question about the `claude` Ctrl+G wrapper
  --no-wrapper            do not install the `claude` wrapper, and do not ask
  --no-desktop-overrides  do not add launcher overrides for Chrome, Brave, Slack, Discord
  --no-start              enable Glance at login but do not start it now
  -h, --help              show this help

Environment (for tests and unusual setups): GLANCE_HOME (use instead of $HOME), GLANCE_SRC,
GLANCE_REPO_URL, GLANCE_TARBALL_URL, GLANCE_SYSTEM_PYTHON, GLANCE_NO_GIT, GLANCE_SKIP_CLONE,
GLANCE_SKIP_VENV, GLANCE_SKIP_FETCH, GLANCE_SKIP_SYSTEMCTL, GLANCE_SKIP_GSETTINGS.
EOF
}

say() { printf '%s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
have_git() { [ -z "${GLANCE_NO_GIT:-}" ] && have git; }

step() {
    STEP=$((STEP + 1))
    printf '\n==> [%d/8] %s\n' "$STEP" "$*"
}

# ---------------------------------------------------------------------- options and paths

parse_args() {
    FROM_LOCAL=0 WITH_APT=0 YES=0 NO_WRAPPER=0 NO_OVERRIDES=0 NO_START=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --from-local) FROM_LOCAL=1 ;;
            --with-apt) WITH_APT=1 ;;
            --yes | -y) YES=1 ;;
            --no-wrapper) NO_WRAPPER=1 ;;
            --no-desktop-overrides) NO_OVERRIDES=1 ;;
            --no-start) NO_START=1 ;;
            -h | --help) usage; exit 0 ;;
            *) usage >&2; die "unknown option: $1" ;;
        esac
        shift
    done
}

# The directory this script sits in; fails when it came on stdin (curl | bash).
script_dir() {
    local src="${BASH_SOURCE[0]:-}"
    if [ -z "$src" ] || [ ! -f "$src" ]; then return 1; fi
    (cd "$(dirname "$src")" && pwd -P)
}

is_checkout() {
    [ -f "$1/pyproject.toml" ] && [ -f "$1/packaging/glance.service" ] \
        && [ -f "$1/scripts/fetch-harper.sh" ]
}

setup_paths() {
    if [ -n "${GLANCE_HOME:-}" ]; then
        # A stand-in home must be self-contained: no XDG directory may point back at the real one.
        HOME="$GLANCE_HOME"
        export HOME
        unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME ZDOTDIR
    fi
    [ -d "$HOME" ] || die "home directory $HOME does not exist"
    SYS_PY="${GLANCE_SYSTEM_PYTHON:-/usr/bin/python3}"
    DATA_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}"
    DATA_DIR="$DATA_ROOT/glance"
    MANIFEST="$DATA_DIR/install-manifest.json"
    BIN_DIR="$HOME/.local/bin"
    UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/glance.service"
    ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"
    BIN_NAMES=(glance glanced glance-capture glance-edit)

    local here=""
    here="$(script_dir)" || here=""
    if [ -n "$here" ] && is_checkout "$here"; then
        FROM_LOCAL=1
        SRC="$here"
    elif [ "$FROM_LOCAL" = 1 ]; then
        die "--from-local needs a Glance checkout: run it as ./install.sh inside one"
    else
        SRC="${GLANCE_SRC:-$DATA_ROOT/glance-src}"  # not under $DATA_DIR: uninstall deletes that
    fi
    VENV="$SRC/.venv"
}

# ---------------------------------------------------------------------- manifest

# manifest_get KEY: a scalar (booleans as 1/0), or one list item per line (symlinks as
# "path<TAB>target"); prints nothing when the manifest or the key is absent.
manifest_get() {
    [ -f "$MANIFEST" ] || return 0
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
' "$MANIFEST" "$1"
}

load_previous() {
    CLONED="$(manifest_get cloned)"
    VENV_CREATED="$(manifest_get venv_created)"
    WRAPPER_INSTALLED="$(manifest_get wrapper_installed)"
    ZSHRC_EXISTED="$(manifest_get zshrc_existed)"
    mapfile -t CREATED_DIRS < <(manifest_get created_dirs)
    mapfile -t PREV_LINKS < <(manifest_get symlinks)
    HAD_MANIFEST=0
    [ -f "$MANIFEST" ] && HAD_MANIFEST=1
    : "${CLONED:=0}" "${VENV_CREATED:=0}" "${WRAPPER_INSTALLED:=0}"
    # "We made it" is only true of the source the record is about: never let an older clone's
    # flag mark a developer's own checkout for deletion.
    local before
    before="$(manifest_get src_dir)"
    if [ "$before" != "$SRC" ]; then
        if [ "$CLONED" = 1 ] && [ -d "$before" ]; then
            SUPERSEDED="$before"
        fi
        CLONED=0
        VENV_CREATED=0
    fi
    LINKS=()
    UNIT_WRITTEN=""
}

# Rewritten after every step that creates something, so a run that stops half-way still
# leaves uninstall.sh an exact record.
write_manifest() {
    GM_SRC="$SRC" GM_CLONED="$CLONED" GM_VENV="$VENV" GM_VENV_CREATED="$VENV_CREATED" \
    GM_UNIT="$UNIT_WRITTEN" GM_ZSHRC="$ZSHRC" GM_ZSHRC_EXISTED="$ZSHRC_EXISTED" \
    GM_WRAPPER="$WRAPPER_INSTALLED" \
    GM_LINKS="$(printf '%s\n' "${LINKS[@]}")" GM_DIRS="$(printf '%s\n' "${CREATED_DIRS[@]}")" \
        "$SYS_PY" -c '
import json, os, sys
e = os.environ
lines = lambda k: [x for x in e[k].split("\n") if x]
flag = lambda k: e[k] == "1"
doc = {
    "version": 1,
    "src_dir": e["GM_SRC"],
    "cloned": flag("GM_CLONED"),
    "venv": e["GM_VENV"],
    "venv_created": flag("GM_VENV_CREATED"),
    "symlinks": [dict(zip(("path", "target"), x.split("\t", 1))) for x in lines("GM_LINKS")],
    "unit": e["GM_UNIT"] or None,
    "zshrc": e["GM_ZSHRC"],
    "zshrc_existed": flag("GM_ZSHRC_EXISTED") if e["GM_ZSHRC_EXISTED"] else None,
    "wrapper_installed": flag("GM_WRAPPER"),
    "created_dirs": lines("GM_DIRS"),
}
tmp = sys.argv[1] + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2)
    f.write("\n")
os.replace(tmp, sys.argv[1])
' "$MANIFEST"
}

# mkdir -p that remembers each directory it had to create, so uninstall can remove them.
ensure_dir() {
    local d="$1" missing=()
    while [ ! -d "$d" ]; do
        missing=("$d" "${missing[@]}")
        d="$(dirname "$d")"
    done
    mkdir -p "$1"
    CREATED_DIRS+=("${missing[@]}")
}

# ---------------------------------------------------------------------- step 1: prerequisites

# Prints the apt packages for whatever $SYS_PY cannot import. gi.require_version only looks
# for the typelib: nothing is initialised and no display is opened.
missing_packages() {
    GM_NEED_VENV="$1" "$SYS_PY" -c '
import os, sys
need = []
try:
    import gi
except Exception:
    gi = None
    need.append("python3-gi")
for ns, ver, pkg in (
    ("Gtk", "3.0", "gir1.2-gtk-3.0"), ("Atspi", "2.0", "gir1.2-atspi-2.0"),
    ("Wnck", "3.0", "gir1.2-wnck-3.0"), ("Notify", "0.7", "gir1.2-notify-0.7"),
    ("Secret", "1", "gir1.2-secret-1"),
):
    try:
        gi.require_version(ns, ver)
    except Exception:
        need.append(pkg)
if os.environ["GM_NEED_VENV"] == "1":
    try:
        import ensurepip, venv
    except Exception:
        need.append("python3-venv")
print(" ".join(need))
sys.exit(1 if need else 0)
' || true
}

tray_missing() {
    ! "$SYS_PY" -c 'import gi; gi.require_version("AyatanaAppIndicator3", "0.1")' 2>/dev/null
}

# systemd's ExecStart= splits on whitespace and expands %-specifiers, and the venv's own
# scripts carry the path in their #! line: a path with either cannot be run reliably.
refuse_awkward_path() {
    case "$2" in
        *[[:space:]%]*) die "$1 $2 contains a space or a '%', which the systemd unit cannot" \
            "run. $3" ;;
    esac
}

check_prerequisites() {
    step "Checking the system"
    [ "${XDG_SESSION_TYPE:-}" = "x11" ] || die "Glance needs an X11 session, and this one is" \
        "'${XDG_SESSION_TYPE:-unknown}'. Log out, pick \"Ubuntu on Xorg\" at the login screen," \
        "and run the installer again."
    local arch
    arch="$(uname -m)"
    [ "$arch" = "x86_64" ] || die "Glance's grammar engine (harper-ls) is only packaged for" \
        "x86_64 Linux, and this machine is $arch."
    local pyver=""
    if [ -x "$SYS_PY" ]; then
        pyver="$("$SYS_PY" -c 'import sys; v = sys.version_info[:2]; print("%d.%d" % v)
sys.exit(0 if (3, 10) <= v < (3, 14) else 1)' 2>/dev/null)" || {
            die "Glance needs the system Python ($SYS_PY) at version 3.10, 3.11, 3.12 or" \
                "3.13, and this one is ${pyver:-unknown}."
        }
    else
        die "Glance needs the system Python ($SYS_PY), version 3.10 to 3.13; it is not installed" \
            "(sudo apt install python3)."
    fi
    have curl || die "curl is needed (sudo apt install curl)."
    refuse_awkward_path "Your home directory" "$HOME" \
        "Glance cannot be installed for this account."
    refuse_awkward_path "Glance's environment" "$VENV" \
        "Install from a checkout whose path has neither, or set GLANCE_SRC to such a directory."
    if [ -z "${GLANCE_SKIP_SYSTEMCTL:-}" ]; then
        local senv
        senv="$(systemctl --user show-environment </dev/null 2>/dev/null)" || die "no systemd" \
            "user manager is reachable (systemctl --user show-environment failed). Glance runs" \
            "as a systemd user service: run the installer from your desktop session's terminal."
        case "$senv" in
            *DISPLAY=*) ;;
            *) note "warning: your systemd user manager has no DISPLAY yet; if glance.service" \
                "does not start, log out and in again" ;;
        esac
    fi

    local need_venv=1 tray="gir1.2-ayatanaappindicator3-0.1" missing
    if have uv || [ -n "${GLANCE_SKIP_VENV:-}" ]; then need_venv=0; fi
    missing="$(missing_packages "$need_venv")"
    if [ "$WITH_APT" = 1 ]; then
        local -a pkgs
        read -r -a pkgs <<<"$missing"
        if tray_missing; then pkgs+=("$tray"); fi
        if [ ${#pkgs[@]} -gt 0 ]; then
            have apt-get || die "--with-apt needs apt-get (Debian, Ubuntu and derivatives)," \
                "and this system has none. Install the equivalents of these packages with" \
                "your package manager and run the installer again: ${pkgs[*]}"
            note "sudo apt-get install -y ${pkgs[*]}"
            sudo apt-get install -y "${pkgs[@]}" || die "apt-get failed; nothing was installed."
            missing="$(missing_packages "$need_venv")"
        fi
    fi
    if [ -n "$missing" ]; then
        die "system packages are missing. Install them and run the installer again:

    sudo apt install $missing

(or pass --with-apt and the installer runs that for you)"
    fi
    note "X11 session, $("$SYS_PY" --version 2>&1), GTK 3, AT-SPI, Wnck: ok"
    if tray_missing; then
        note "optional, for the tray icon: sudo apt install $tray"
    fi
}

# ---------------------------------------------------------------------- step 5: symlinks

# True when the symlink $1 is ours: it points at $2, or the previous install recorded it.
is_our_link() {
    local now line
    now="$(readlink "$1")"
    [ "$now" = "$2" ] && return 0
    for line in "${PREV_LINKS[@]}"; do
        [ "$line" = "$1"$'\t'"$now" ] && return 0
    done
    return 1
}

# Run before anything is written, and again just before linking.
refuse_foreign_binaries() {
    local name link
    for name in "${BIN_NAMES[@]}"; do
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
    for name in "${BIN_NAMES[@]}"; do
        if [ -x "$VENV/bin/$name" ]; then
            ln -sfn "$VENV/bin/$name" "$BIN_DIR/$name"
            LINKS+=("$BIN_DIR/$name"$'\t'"$VENV/bin/$name")
            note "$name"
        else
            rm -f "$BIN_DIR/$name"  # ours (checked above), left by an older version
            note "$name: not part of this version, skipped"
        fi
    done
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) note "warning: $BIN_DIR is not on your PATH; log out and in, or run $BIN_DIR/glance ..." ;;
    esac
    write_manifest
}

# ---------------------------------------------------------------------- steps 2-3: source, venv

fetch_tarball() {
    local url="${GLANCE_TARBALL_URL:-https://github.com/Btocode/glance/archive/refs/heads/main.tar.gz}"
    note "git is not installed: downloading $url"
    have tar || die "neither git nor tar is installed (sudo apt install git)."
    # Unpacked next to $SRC (same filesystem) and renamed into place only once complete, so a
    # failed or cut download never leaves a half-written source behind.
    ensure_dir "$(dirname "$SRC")"
    local new old
    new="$(mktemp -d "$SRC.new.XXXXXX")"
    if ! curl -fsSL --retry 3 "$url" </dev/null | tar -xz --strip-components=1 -C "$new" \
        || ! is_checkout "$new"; then
        rm -rf "$new"
        die "could not download Glance from $url"
    fi
    chmod 755 "$new"  # mktemp -d makes it 0700
    if [ -e "$SRC" ] && [ -n "$(ls -A "$SRC")" ]; then
        # A refresh: keep the venv (its paths name $SRC, which is where it ends up again).
        if [ -d "$SRC/.venv" ]; then rm -rf "$new/.venv"; mv "$SRC/.venv" "$new/.venv"; fi
        old="$(mktemp -u "$SRC.old.XXXXXX")"
        mv -T "$SRC" "$old"
        mv -T "$new" "$SRC"
        rm -rf "$old"
    else
        mv -T "$new" "$SRC"  # rename(2) replaces an empty directory
    fi
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
    if [ -d "$SRC/.git" ]; then
        have_git || die "$SRC is a git clone but git is not installed (sudo apt install git)."
        note "updating $SRC"
        GIT_TERMINAL_PROMPT=0 git -C "$SRC" pull --ff-only --quiet </dev/null \
            || die "could not fast-forward $SRC. Resolve it there (git status), then run again."
    elif [ "$fresh" = 1 ] && have_git; then
        note "cloning $url into $SRC"
        ensure_dir "$(dirname "$SRC")"
        GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 "$url" "$SRC" </dev/null || die "could not clone $url"
    else
        fetch_tarball
    fi
    if [ "$fresh" = 1 ]; then CLONED=1; fi
    is_checkout "$SRC" || die "$SRC does not look like Glance after the download."
}

make_venv() {
    step "Building the Python environment"
    if [ -n "${GLANCE_SKIP_VENV:-}" ]; then
        note "skipped (GLANCE_SKIP_VENV)"
    else
        if [ -x "$VENV/bin/python" ]; then
            note "$VENV exists, kept"
        elif have uv; then
            uv venv --python "$SYS_PY" --system-site-packages "$VENV" </dev/null
            VENV_CREATED=1
        else
            "$SYS_PY" -m venv --system-site-packages "$VENV" </dev/null \
                || die "could not create $VENV. Run: sudo apt install python3-venv"
            VENV_CREATED=1
        fi
        write_manifest
        if have uv; then
            uv pip install --python "$VENV/bin/python" -e "$SRC" </dev/null
        else
            "$VENV/bin/python" -m pip install --quiet -e "$SRC" </dev/null
        fi
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
    local bin="$DATA_DIR/bin/harper-ls" want
    want="$(sed -n 's/^BINARY_SHA256="\([0-9a-f]\{64\}\)"$/\1/p' "$SRC/scripts/fetch-harper.sh")"
    if [ -n "$want" ] && [ -x "$bin" ] && [ "$(sha256sum "$bin" | cut -d' ' -f1)" = "$want" ]; then
        note "already installed, checksum matches"
        return
    fi
    bash "$SRC/scripts/fetch-harper.sh" </dev/null
}

# ---------------------------------------------------------------------- step 6: systemd

user_systemctl() {
    if [ -n "${GLANCE_SKIP_SYSTEMCTL:-}" ]; then
        note "skipped (GLANCE_SKIP_SYSTEMCTL): systemctl --user $*"
        return 0
    fi
    systemctl --user "$@" </dev/null
}

install_unit() {
    step "Installing the systemd user unit"
    local text
    text="$(<"$SRC/packaging/glance.service")"
    ensure_dir "$(dirname "$UNIT")"
    printf '%s\n' "${text//@VENV@/"$VENV"}" >"$UNIT"
    UNIT_WRITTEN="$UNIT"
    write_manifest
    note "$UNIT"
    user_systemctl daemon-reload
    if [ "$NO_START" = 1 ]; then
        user_systemctl enable glance.service
        note "enabled; it starts at your next login (--no-start)"
    else
        user_systemctl enable --now glance.service
        if [ "$HAD_MANIFEST" = 1 ]; then
            user_systemctl restart glance.service  # an update: run the new code
        fi
    fi
}

# ---------------------------------------------------------------------- step 7: doctor --fix

# Asks on the terminal, not stdin (stdin is the script under curl | bash). No terminal: no.
ask() {
    local reply=""
    [ "$YES" = 1 ] && return 0
    { : </dev/tty; } 2>/dev/null || return 1
    printf '%s [y/N] ' "$1" >/dev/tty
    IFS= read -r reply </dev/tty || return 1
    case "$reply" in
        y | Y | yes | Yes | YES) return 0 ;;
        *) return 1 ;;
    esac
}

# The basename of the login shell: $SHELL, which login sets from the passwd entry.
login_shell() {
    local sh="${SHELL:-}"
    if [ -z "$sh" ]; then sh="$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f7)"; fi
    printf '%s\n' "${sh##*/}"
}

# Sets WANT_WRAPPER. The wrapper is a marked claude() function in .zshrc that points Ctrl+G
# at glance-edit for that one process; $EDITOR itself is never changed.
decide_wrapper() {
    WANT_WRAPPER=0
    local state
    state="$("$VENV/bin/glance" wrapper status </dev/null 2>/dev/null)" || state=""
    case "$state" in
        installed)
            WANT_WRAPPER=1
            note "claude wrapper: already in $ZSHRC"
            ;;
        absent)
            if [ "$NO_WRAPPER" = 1 ]; then
                note "claude wrapper: not installed (--no-wrapper)"
            elif ! have claude; then
                note "claude wrapper: not offered, the claude CLI is not installed." \
                    "Add it later with: glance wrapper install"
            elif [ "$(login_shell)" != zsh ]; then
                note "claude wrapper: not offered, your login shell is not zsh." \
                    "Add it later with: glance wrapper install"
            elif ask "Add a claude() wrapper to $ZSHRC so Ctrl+G in Claude Code opens Glance? Your \$EDITOR stays as it is."; then
                WANT_WRAPPER=1
                if [ -z "$ZSHRC_EXISTED" ]; then
                    ZSHRC_EXISTED=0
                    if [ -e "$ZSHRC" ]; then ZSHRC_EXISTED=1; fi
                fi
                WRAPPER_INSTALLED=1
                write_manifest
            else
                note "claude wrapper: not installed. Add it later with: glance wrapper install"
            fi
            ;;
        conflict)
            note "claude wrapper: $ZSHRC already defines claude, left untouched (see below)"
            ;;
        *)
            note "claude wrapper: could not check $ZSHRC, left untouched"
            ;;
    esac
}

run_doctor() {
    step "Setting up (glance doctor --fix)"
    decide_wrapper
    local -a skip=(--skip harper)  # step 4 fetched it
    if [ "$NO_OVERRIDES" = 1 ]; then skip+=(--skip desktop_flags); fi
    if [ "$WANT_WRAPPER" = 0 ]; then skip+=(--skip wrapper); fi
    REPORT="$(mktemp)"
    DOCTOR_ERR="$(mktemp)"
    trap 'rm -f "$REPORT" "$DOCTOR_ERR"' EXIT
    DOCTOR_RC=0
    "$VENV/bin/glance" doctor --fix "${skip[@]}" --json </dev/null >"$REPORT" 2>"$DOCTOR_ERR" \
        || DOCTOR_RC=$?
    # Exit 1 with a full JSON report only says a check is still red (step 8 shows which).
    # Anything else, including a traceback's exit 1 with no report, is a crash.
    if [ "$DOCTOR_RC" -gt 1 ] || ! "$SYS_PY" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    sys.exit(0 if isinstance(json.load(f), list) else 1)
' "$REPORT" 2>/dev/null; then
        write_manifest
        tail -n 20 "$DOCTOR_ERR" >&2
        die "glance doctor --fix crashed (exit $DOCTOR_RC; its output is above). Glance is" \
            "installed but not set up: fix the error, then run the installer again, or" \
            "$SRC/uninstall.sh to remove it."
    fi
    if [ "$WRAPPER_INSTALLED" = 1 ] && ! grep -qF '>>> glance claude wrapper >>>' "$ZSHRC" 2>/dev/null; then
        WRAPPER_INSTALLED=0
    fi
    write_manifest
}

# ---------------------------------------------------------------------- step 8: report

print_report() {
    step "Result (glance doctor)"
    "$VENV/bin/python" -c '
import json, re, sys
from glance.backends.base import CONNECT
from glance.doctor.checks import CheckResult
from glance.doctor.runner import render

with open(sys.argv[1], encoding="utf-8") as f:
    results = [CheckResult(**d) for d in json.load(f)]
print(render(results))
by_id = {r.id: r for r in results}
running = by_id.get("running_flags")
if running and running.status != "ok":
    m = re.match(r"Restart (.+?) to enable", running.detail)
    print("\nRestart: " + (m.group(1) if m else running.detail))
backend = by_id.get("backend")
if backend and backend.status != "ok":
    labels = {"api": "API key"}
    print("\nConnect an AI (any one of these; grammar fixes work without it):")
    for name, command in CONNECT.items():
        print(f"    {labels.get(name, name):<10}{command}")
' "$REPORT" </dev/null || {
        note "could not read the doctor report; run: glance doctor"
        cat "$REPORT"
    }
    if [ "$DOCTOR_RC" = 1 ]; then
        say ""
        say "Some checks above are still red (✗). Fix what they say, then run: glance doctor"
    fi
    say ""
    say "Glance is installed."
    say "    glance doctor    check the setup again at any time"
    say "    glance status    what Glance is doing right now"
    say "    uninstall        $SRC/uninstall.sh"
    if [ -n "${SUPERSEDED:-}" ]; then
        say ""
        say "Glance now runs from $SRC. The earlier copy in $SUPERSEDED is no longer used;"
        say "delete it when you like."
    fi
}

# ---------------------------------------------------------------------- main

main() {
    STEP=0
    parse_args "$@"
    setup_paths
    check_prerequisites            # 1
    load_previous
    refuse_foreign_binaries
    get_source                     # 2
    ensure_dir "$DATA_DIR"
    write_manifest
    make_venv                      # 3
    fetch_harper                   # 4
    install_links                  # 5
    install_unit                   # 6
    run_doctor                     # 7
    print_report                   # 8
}

{ main "$@"; exit $?; }
