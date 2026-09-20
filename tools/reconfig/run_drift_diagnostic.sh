#!/bin/bash
# Run inside the CentOS6 container, AFTER rebuilding with the debug print added to
# CacheCntlr::reconfigure() (see BUG_static_arm_drift.md). Short/fast: barnes-small-4,
# 5M instructions instead of 1B, same max-resources starting config that showed 100%
# drift. Captures stderr (where the debug prints land) to a log file for inspection.

set -eu
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
cd /root/benchmarks

OUTDIR=$SNIPER_ROOT/results/_drift_diagnostic
mkdir -p "$OUTDIR"

/root/benchmarks/run-sniper --benchmarks splash2-barnes-small-4 -n 2 -c gainestown \
  -s stop-by-icount:5000000 \
  -d "$OUTDIR" \
  -g perf_model/branch_predictor/num_ways=4 \
  -g perf_model/l2_cache/prefetcher/prefetch_on_prefetch_hit=true \
  -g perf_model/l2_cache/prefetcher/simple/flows=16 \
  -g perf_model/l2_cache/prefetcher/simple/num_prefetches=4 \
  -g perf_model/l2_cache/prefetcher/simple/stop_at_page_boundary=false \
  -g perf_model/l2_cache/prefetcher/simple/flows_per_core=false \
  -g general/max_instructions=5000000 \
  -g perf_model/l2_cache/cache_size=1024 \
  -g perf_model/l3_cache/cache_size=16384 \
  -g perf_model/branch_predictor/num_entries=4096 \
  -g perf_model/l2_cache/prefetcher=simple \
  -greconfig/enabled=true \
  -greconfig/python_hook_script=$SNIPER_ROOT/tools/reconfig/noop_predict.py \
  -greconfig/mcpat_script_path=$SNIPER_ROOT/tools/mcpat.py \
  -greconfig/decision_log_path="$OUTDIR/sniper_reconfig_decisions.csv" \
  -greconfig/live_config_path="$OUTDIR/sniper_reconfig_live.cfg" \
  2> "$OUTDIR/reconfig_debug.log"

echo "Done. Debug trace:"
grep "reconfig-debug" "$OUTDIR/reconfig_debug.log" | head -20
echo "..."
echo "Decision log:"
cat "$OUTDIR/sniper_reconfig_decisions.csv"
