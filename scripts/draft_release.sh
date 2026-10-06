#!/usr/bin/env bash
# Draft the GitHub release for the version in pyproject.toml, with its CHANGELOG section as the
# notes. Nothing ships until someone reads the draft and presses "Publish release"; publishing
# runs .github/workflows/release.yml (build, test the wheel, attach files, PyPI, move v0).
#
#   scripts/draft_release.sh            # drafts vX.Y.Z on origin/main
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

version=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")
tag="v$version"

git fetch -q origin main --tags
main_version=$(git show origin/main:pyproject.toml | python3 -c "import sys, tomllib; print(tomllib.loads(sys.stdin.read())['project']['version'])")
[ "$main_version" = "$version" ] || { echo "origin/main is at $main_version, not $version: merge the version bump first" >&2; exit 1; }
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "$tag already exists" >&2; exit 1
fi

notes=$(mktemp)
awk -v v="$version" '
  $0 ~ "^## \\[" v "\\]" {p=1; next}
  p && /^## \[/ {exit}
  p {print}
' CHANGELOG.md > "$notes"
[ -s "$notes" ] || { echo "CHANGELOG.md has no [$version] section" >&2; exit 1; }

gh release create "$tag" --draft --target main --title "$tag" --notes-file "$notes"
echo "Drafted $tag. Read it on GitHub, then press \"Publish release\" to ship."
