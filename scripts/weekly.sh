#!/usr/bin/env bash
# One-shot weekly report. Usage: ./scripts/weekly.sh [week]
# Prints to the terminal and saves a copy under reports/.
set -euo pipefail
cd "$(dirname "$0")/.."
W=${1:-}
ARG=""; [ -n "$W" ] && ARG="--week $W"
OUT="reports/week-${W:-current}-$(date +%Y-%m-%d).txt"
{
  python3 scripts/ff.py lineup  $ARG
  python3 scripts/ff.py matchup $ARG
  python3 scripts/ff.py waivers $ARG -n 12
  python3 scripts/needs.py      $ARG
} | tee "$OUT"
echo "Saved to $OUT"
