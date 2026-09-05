#!/usr/bin/env bash
set -euo pipefail
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=${RIVERBANK_PAPER_RADAR_ROOT:-$HOME/riverbank-edge-os/apps/paper-radar}
if [ -f "$PWD/scripts/collect.py" ]; then
  project_dir=$PWD
fi
if [ ! -f "$project_dir/scripts/collect.py" ]; then
  project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
fi
cd "$project_dir"
python_bin=${RIVERBANK_PAPER_RADAR_PYTHON:-$project_dir/.venv/bin/python}
if [ ! -x "$python_bin" ]; then
  python_bin=/usr/bin/python3
fi
exec "$python_bin" "$project_dir/scripts/collect.py" --force-all
