#!/usr/bin/env bash
# Record docs/assets/demo.gif: the overlay probes' scenes on a private Xvfb (never your
# display), turned into a GIF with ffmpeg, or with Pillow when ffmpeg is missing.
#
#   scripts/make-demo.sh [OUT.gif]
#
# Needs Xvfb, the GTK 3 / PyGObject / python-xlib that Glance needs, and a Python that can
# import them (PYTHON, else .venv/bin/python, else python3). Everything temporary, HOME
# included, lives in a throwaway directory.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUT="${1:-$ROOT/docs/assets/demo.gif}"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY=python3; fi
fi
command -v Xvfb >/dev/null || { echo "make-demo: Xvfb is not installed" >&2; exit 1; }

WORK="$(mktemp -d)"
XVFB_PID=""
cleanup() {
    if [ -n "$XVFB_PID" ]; then kill "$XVFB_PID" 2>/dev/null || true; fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# A private server: the display number comes back on a pipe, so no guessing or collisions.
mkfifo "$WORK/displayfd"
Xvfb -displayfd 3 -screen 0 1280x800x24 -nolisten tcp 3>"$WORK/displayfd" 2>/dev/null &
XVFB_PID=$!
read -r -t 10 NUM <"$WORK/displayfd" || { echo "make-demo: Xvfb did not start" >&2; exit 1; }

mkdir -p "$WORK/home" "$WORK/run" "$WORK/frames"
chmod 700 "$WORK/run"
env -i PATH="$PATH" LANG="${LANG:-C.UTF-8}" HOME="$WORK/home" XDG_RUNTIME_DIR="$WORK/run" \
    XDG_CONFIG_HOME="$WORK/home/.config" XDG_DATA_HOME="$WORK/home/.local/share" \
    XDG_CACHE_HOME="$WORK/home/.cache" XDG_STATE_HOME="$WORK/home/.local/state" \
    DISPLAY=":$NUM" GDK_BACKEND=x11 NO_AT_BRIDGE=1 PYTHONPATH="$ROOT/src" \
    "$PY" "$ROOT/scripts/demo_frames.py" record "$WORK/frames"

"$PY" "$ROOT/scripts/demo_frames.py" assemble "$WORK/frames" "$OUT"
size="$(wc -c <"$OUT")"
if [ "$size" -gt 3000000 ]; then
    echo "make-demo: $OUT is $size bytes; keep it under 3 MB" >&2
    exit 1
fi
