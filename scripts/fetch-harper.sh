#!/usr/bin/env bash
# Download harper-ls into ~/.local/share/glance/bin after verifying pinned SHA-256 sums.
# Linux x86_64 and macOS (Apple silicon and Intel). bash 3.2-safe (macOS's /bin/bash).
set -euo pipefail

VERSION="3.3.0"
ASSET="harper-ls-x86_64-unknown-linux-gnu.tar.gz"
TARBALL_SHA256="db9980a3387b48b4c99cac6402fdbcaff121a1724410ebbe554500df7fa7af88"
BINARY_SHA256="c62ae24296005b835875f7e53238e05969feab68647f1c590d28c5de7fd8ff26"
# macOS assets of the same release (tarball digests match GitHub's release metadata).
DARWIN_ARM64_ASSET="harper-ls-aarch64-apple-darwin.tar.gz"
DARWIN_ARM64_TARBALL_SHA256="bd640c5a1e80f61e602b9cde6c40d29612ce791528ae5081fbfd80a57308cd9f"
DARWIN_ARM64_BINARY_SHA256="cbe584977b11c7859743401eeef58c193e2debe97eec17103e661496e2499ba7"
DARWIN_X86_64_ASSET="harper-ls-x86_64-apple-darwin.tar.gz"
DARWIN_X86_64_TARBALL_SHA256="871a6116635ca37020de2d48a907f637a40355d97641dc56511fa5f85a65988d"
DARWIN_X86_64_BINARY_SHA256="701a2c49e15d98422fcafd973d7ecd0dd7d965b4bf0c4e5a81998bf3fca6c87e"
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/glance/bin"

case "${GLANCE_FETCH_PLATFORM:-$(uname -s)-$(uname -m)}" in
    Darwin-arm64 | Darwin-aarch64)
        ASSET="$DARWIN_ARM64_ASSET"
        TARBALL_SHA256="$DARWIN_ARM64_TARBALL_SHA256"
        BINARY_SHA256="$DARWIN_ARM64_BINARY_SHA256"
        ;;
    Darwin-x86_64)
        ASSET="$DARWIN_X86_64_ASSET"
        TARBALL_SHA256="$DARWIN_X86_64_TARBALL_SHA256"
        BINARY_SHA256="$DARWIN_X86_64_BINARY_SHA256"
        ;;
esac
URL="https://github.com/Automattic/harper/releases/download/v${VERSION}/${ASSET}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}
check() { [ "$(sha256_of "$2")" = "$1" ]; }

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
