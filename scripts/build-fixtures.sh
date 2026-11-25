#!/usr/bin/env bash
# Rebuild tests/acceptance/fixtures/dist/*.js from the pinned editors (build time only;
# the bundles are committed, so running the tests needs no Node).
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh"
nvm use 24 >/dev/null
cd "$here/tests/acceptance/fixtures"
npm ci --no-audit --no-fund
npx esbuild src/*.js --bundle --minify --format=iife \
  --define:process.env.NODE_ENV='"production"' --outdir=dist
# Record what the bundles were built from; tests/acceptance/test_harness.py checks it.
python3 - <<'PY'
import hashlib, pathlib
f = pathlib.Path(".")
names = sorted([*(p.as_posix() for p in f.glob("src/*.js")), "bridge.js", "package-lock.json"])
h = hashlib.sha256()
for n in names:
    h.update(n.encode() + b"\0" + (f / n).read_bytes() + b"\0")
pathlib.Path("dist/.src-hash").write_text(h.hexdigest() + "\n")
PY
