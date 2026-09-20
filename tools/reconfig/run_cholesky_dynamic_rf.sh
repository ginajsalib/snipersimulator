#!/bin/bash
# Run inside the CentOS6 container, from /root/benchmarks. The dynamic_rf arm: the real
# RF-driven reconfiguration, via .reconfig_bridge_shim.sh -> host watcher ->
# rf_predict_ncore.py. Companion to run_cholesky_baselines.sh (the 3 static arms);
# identical in every respect except reconfig/python_hook_script.
#
# PREREQUISITE: the host-side watcher must be running AND actually able to load the
# model bundle. Check before starting, or every interval silently degrades to
# predict_failed (the shim reports "timed out waiting for host-side predictor"):
#   pgrep -f reconfig_bridge_watch
#   tail /tmp/reconfig_bridge_watch.log     # must NOT say "rf_predict_ncore.py failed"

set -eu
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
CFG_DIR=/root/benchmarks
cd "$CFG_DIR"

OUTDIR=$SNIPER_ROOT/results/cholesky/dynamic_rf
# Same reason as run_cholesky_baselines.sh: McPAT never overwrites power-* files (each
# interval's t0/t1 window makes a unique name), so re-running into a dirty directory
# silently mixes two runs' samples. Only decisions.csv self-truncates.
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

/root/benchmarks/run-sniper --benchmarks splash2-cholesky-small-4 -n 2 -c gainestown \
  -s stop-by-icount:1000000000 \
  -d "$OUTDIR" \
  -g perf_model/branch_predictor/num_ways=4 \
  -g perf_model/l2_cache/prefetcher/prefetch_on_prefetch_hit=true \
  -g perf_model/l2_cache/prefetcher/simple/flows=16 \
  -g perf_model/l2_cache/prefetcher/simple/num_prefetches=4 \
  -g perf_model/l2_cache/prefetcher/simple/stop_at_page_boundary=false \
  -g perf_model/l2_cache/prefetcher/simple/flows_per_core=false \
  -g general/max_instructions=1000000000 \
  -greconfig/enabled=true \
  -greconfig/python_hook_script=$SNIPER_ROOT/.reconfig_bridge_shim.sh \
  -greconfig/mcpat_script_path=$SNIPER_ROOT/tools/mcpat.py \
  -greconfig/decision_log_path="$OUTDIR/sniper_reconfig_decisions.csv" \
  -greconfig/live_config_path="$OUTDIR/sniper_reconfig_live.cfg"

echo
echo "Done. Sanity-check that predictions actually landed (should be 0):"
grep -c "predict_failed\|parse_failed" "$OUTDIR/sniper_reconfig_decisions.csv" || true
