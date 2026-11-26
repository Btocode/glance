#!/usr/bin/env bash
# Build glance_<version>_amd64.deb into dist/ (needs network for the wheels, uv, and nfpm on
# PATH). Run it on the oldest supported Ubuntu (22.04, Python 3.10) so the wheelhouse holds
# every dependency that newer Pythons might not need, never one they lack.
#
#   packaging/debian/build-deb.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd "$HERE/../.." && pwd -P)"
VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO/pyproject.toml" | head -n 1)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT="$REPO/dist"
export VERSION STAGE

command -v nfpm >/dev/null || { echo "build-deb: nfpm is not installed" >&2; exit 1; }
command -v uv >/dev/null || { echo "build-deb: uv is not installed" >&2; exit 1; }

mkdir -p "$STAGE/opt/glance/wheels" "$STAGE/usr/share/doc/glance" "$OUT"

# Glance's wheel and every dependency as wheels (pure Python), for an offline postinst
uv build --wheel --out-dir "$STAGE/build" "$REPO"
python3 -m pip wheel --quiet --wheel-dir "$STAGE/opt/glance/wheels" "$STAGE"/build/glance-*.whl
rm -rf "$STAGE/build"

# glance_core, pinned by scripts/core-wheels.sha256 (never an unpinned wheel)
line="$(grep -E '^[0-9a-f]{64}  glance_core-.*manylinux.*_x86_64\.whl$' \
    "$REPO/scripts/core-wheels.sha256" | tail -n 1)"
sha="${line%%  *}" name="${line#*  }" corever="$(printf '%s' "$name" | cut -d- -f2)"
curl -fsSL --retry 3 -o "$STAGE/opt/glance/wheels/$name" \
    "https://github.com/Btocode/glance/releases/download/core-v$corever/$name"
echo "$sha  $STAGE/opt/glance/wheels/$name" | sha256sum -c -

# The support tree `glance setup` runs (install.sh --setup-only) and reads
cp -r "$REPO/install.sh" "$REPO/uninstall.sh" "$REPO/pyproject.toml" "$REPO/scripts" \
    "$REPO/packaging" "$REPO/extension" "$STAGE/opt/glance/"
cp "$REPO/LICENSE" "$STAGE/usr/share/doc/glance/copyright"

cd "$HERE"
nfpm package --config nfpm.yaml --packager deb --target "$OUT/"
ls -l "$OUT"/glance_*"$VERSION"*.deb
