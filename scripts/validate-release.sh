#!/usr/bin/env bash
set -euo pipefail

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cache_dir=$(mktemp -d)
trap 'rm -rf "$cache_dir"' EXIT

PYTHONPYCACHEPREFIX="$cache_dir" python3 -m compileall -q "$repo/apps" "$repo/scripts"
python3 "$repo/scripts/scan_release.py"

while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find "$repo" -path "$repo/build" -prune -o -type f -name '*.sh' -print0)

echo "syntax and release checks passed"
