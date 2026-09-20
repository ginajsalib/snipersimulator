#!/bin/bash
# Run inside the CentOS6 container, right after a run-sniper invocation, to
# triage whether the run's output is trustworthy despite a post-ROI SIFT
# recorder crash (see NEXT_EXPERIMENTS.md's "Triage" section).
#
# Usage: verify_run.sh <OUTDIR> [expected_interval_count]

set -u
OUTDIR="${1:?usage: verify_run.sh <OUTDIR> [expected_interval_count]}"
EXPECTED="${2:-48}"

echo "== $OUTDIR =="

for f in sim.cfg sim.out sim.stats.sqlite3; do
  if [ -s "$OUTDIR/$f" ]; then
    echo "[ok]   $f present and non-empty"
  else
    echo "[FAIL] $f missing or empty"
  fi
done

n_power=$(ls "$OUTDIR"/power-*.txt 2>/dev/null | wc -l)
echo "power-*.txt count: $n_power (expected ~$EXPECTED)"
if [ "$n_power" -lt "$EXPECTED" ]; then
  echo "[WARN] fewer power files than expected -- crash may have cut the tail"
fi

if [ -f "$OUTDIR/sniper_reconfig_decisions.csv" ]; then
  n_rows=$(($(wc -l < "$OUTDIR/sniper_reconfig_decisions.csv") - 1))
  echo "decision-log rows: $n_rows (expected ~$EXPECTED)"
  echo "last decision-log row:"
  tail -1 "$OUTDIR/sniper_reconfig_decisions.csv"
else
  echo "[FAIL] sniper_reconfig_decisions.csv missing"
fi

echo
echo "If all [ok]/counts look sane, run on the host:"
echo "  python2 tools/reconfig/analyze_final_experiment.py summarize --dir $OUTDIR --label cholesky/dynamic_rf"
