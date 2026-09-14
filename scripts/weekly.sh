#!/usr/bin/env bash
# One-shot weekly report. Usage: ./scripts/weekly.sh [week]
set -euo pipefail
cd "$(dirname "$0")/.."
W=${1:-}
ARG=""; [ -n "$W" ] && ARG="--week $W"
python3 scripts/ff.py lineup  $ARG
python3 scripts/ff.py matchup $ARG
python3 scripts/ff.py waivers $ARG -n 12
python3 scripts/needs.py      $ARG
