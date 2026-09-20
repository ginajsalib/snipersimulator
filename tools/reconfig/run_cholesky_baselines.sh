#!/bin/bash
# Run inside the CentOS6 container, from /root/benchmarks, once dynamic_rf's
# cholesky run is confirmed good (see verify_run.sh). Produces the 3 missing
# arms needed for `analyze_final_experiment.py compare --benchmark cholesky`.
#
# Same universal knobs as the dynamic_rf run (num_ways=4, prefetcher tuning,
# -c gainestown only, 1B instructions) so only the *reconfigurable* dimensions
# (L2/L3/BTB/prefetcher) and the hook script differ per arm. All three use
# noop_predict.py (pure stdlib, no bridge needed) so the per-interval
# decision-log + McPAT sampling structure stays identical to dynamic_rf's,
# just with zero actual changes applied each interval.

set -eu
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
CFG_DIR=/root/benchmarks   # run-sniper resolves bare `-c core0,core1` names here (matches
                           # runSniperWithCfg.sh's CFG_DIR, NOT $SNIPER_ROOT)
cd "$CFG_DIR"

COMMON_G=(
  -g perf_model/branch_predictor/num_ways=4
  -g perf_model/l2_cache/prefetcher/prefetch_on_prefetch_hit=true
  -g perf_model/l2_cache/prefetcher/simple/flows=16
  -g perf_model/l2_cache/prefetcher/simple/num_prefetches=4
  -g perf_model/l2_cache/prefetcher/simple/stop_at_page_boundary=false
  -g perf_model/l2_cache/prefetcher/simple/flows_per_core=false
  -g general/max_instructions=1000000000
)

run_arm () {
  local arm="$1"; shift
  local outdir="$SNIPER_ROOT/results/cholesky/$arm"
  # Clear any previous run's output first: run-sniper/McPAT never overwrite power-*
  # files (each interval's t0/t1 window makes a unique filename), only decisions.csv
  # gets truncated on interval 0 -- a re-run into the same dir silently accumulates a
  # second, overlapping batch of power files alongside the fresh one instead of
  # replacing it (bit us once already, see BUG_static_arm_drift.md's cleanup note).
  rm -rf "$outdir"
  mkdir -p "$outdir"
  /root/benchmarks/run-sniper --benchmarks splash2-cholesky-small-4 -n 2 -c gainestown "$@" \
    -s stop-by-icount:1000000000 \
    -d "$outdir" \
    "${COMMON_G[@]}" \
    -greconfig/enabled=true \
    -greconfig/python_hook_script=$SNIPER_ROOT/tools/reconfig/noop_predict.py \
    -greconfig/mcpat_script_path=$SNIPER_ROOT/tools/mcpat.py \
    -greconfig/decision_log_path="$outdir/sniper_reconfig_decisions.csv" \
    -greconfig/live_config_path="$outdir/sniper_reconfig_live.cfg"
}

# --- no_change: pure gainestown/nehalem defaults, same starting point ---
# dynamic_rf actually started from (and mostly stayed at) L2=256KB/core,
# L3=8192KB, BTB=512/512, prefetch=none/none -- exactly gainestown.cfg's
# and nehalem.cfg's un-overridden defaults, so no extra -c/-g needed here.
run_arm no_change

# --- best_static: hindsight-optimal static config from the training sweep ---
# (/tmp/static_vs_optimal.txt cholesky "BEST static": L2=1024/512 L3=16384
# BTB=4096/2048 PF=none/none)
cat <<EOF > "$CFG_DIR/core0.cfg"
[perf_model/branch_predictor]
num_entries = 4096

[perf_model/l2_cache]
prefetcher = none
cache_size = 1024
EOF
cat <<EOF > "$CFG_DIR/core1.cfg"
[perf_model/branch_predictor]
num_entries = 2048

[perf_model/l2_cache]
prefetcher = none
cache_size = 512
EOF
run_arm best_static -c core0,core1 -g perf_model/l3_cache/cache_size=16384

# --- max_resources: ceiling sanity check, max of every dimension ---
cat <<EOF > "$CFG_DIR/core0.cfg"
[perf_model/branch_predictor]
num_entries = 4096

[perf_model/l2_cache]
prefetcher = simple
cache_size = 1024
EOF
cat <<EOF > "$CFG_DIR/core1.cfg"
[perf_model/branch_predictor]
num_entries = 4096

[perf_model/l2_cache]
prefetcher = simple
cache_size = 1024
EOF
run_arm max_resources -c core0,core1 -g perf_model/l3_cache/cache_size=16384

echo "All 3 arms done. Verify each with:"
echo "  bash \$SNIPER_ROOT/tools/reconfig/verify_run.sh \$SNIPER_ROOT/results/cholesky/<arm> 599"
