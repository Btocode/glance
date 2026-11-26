#!/usr/bin/env bash
# Print one release's section of CHANGELOG.md (without its "## [x.y.z]" heading), for the
# GitHub Release notes. Fails when the version has no section.
#
#   scripts/changelog-section.sh 0.4.0 [CHANGELOG.md]
set -euo pipefail

version="${1:?usage: changelog-section.sh VERSION [CHANGELOG]}"
version="${version#v}"
file="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/CHANGELOG.md}"

out="$(awk -v want="## [$version]" '
    index($0, want) == 1 { found = 1; next }
    found && /^## \[/ { exit }
    found && /^\[[^]]+\]: / { exit }
    found { print }
' "$file")"
# trim leading and trailing blank lines
out="$(printf '%s\n' "$out" | sed -e '/./,$!d')"
[ -n "$out" ] || { echo "changelog-section: no section for $version in $file" >&2; exit 1; }
printf '%s\n' "$out"
