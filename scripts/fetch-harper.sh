#!/usr/bin/env bash
# Download harper-ls into ~/.local/share/glance/bin after verifying pinned SHA-256 sums.
set -euo pipefail

VERSION="3.3.0"
ASSET="harper-ls-x86_64-unknown-linux-gnu.tar.gz"
URL="https://github.com/Automattic/harper/releases/download/v${VERSION}/${ASSET}"
TARBALL_SHA256="db9980a3387b48b4c99cac6402fdbcaff121a1724410ebbe554500df7fa7af88"
BINARY_SHA256="c62ae24296005b835875f7e53238e05969feab68647f1c590d28c5de7fd8ff26"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/glance/bin"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

check() { echo "$1  $2" | sha256sum --check --status; }

curl -fsSL --retry 3 -o "$tmp/$ASSET" "$URL"
if ! check "$TARBALL_SHA256" "$tmp/$ASSET"; then
    echo "fetch-harper: checksum mismatch for $ASSET" >&2
    exit 1
fi
tar -xzf "$tmp/$ASSET" -C "$tmp" harper-ls
if ! check "$BINARY_SHA256" "$tmp/harper-ls"; then
    echo "fetch-harper: checksum mismatch for harper-ls binary" >&2
    exit 1
fi
mkdir -p "$DEST_DIR"
install -m 0755 "$tmp/harper-ls" "$DEST_DIR/harper-ls.new"
mv -f "$DEST_DIR/harper-ls.new" "$DEST_DIR/harper-ls"
echo "harper-ls $VERSION installed at $DEST_DIR/harper-ls"
