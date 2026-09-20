#!/bin/bash
# N=4 heterogeneous runner: 2 performance cores (0,1) + 2 efficient cores (2,3).
# Run inside the CentOS6 container, from /root/benchmarks.
#
#   run_n4_hetero.sh <benchmark> <arm>
#     benchmark: barnes | cholesky | fft | radiosity | radix | water.nsq | ...
#     arm:       no_change | dynamic_rf | max_resources | best_static
#
# Heterogeneity uses run-sniper's own mechanism: `-c <f0>,<f1>,<f2>,<f3>` makes file i
# the config for core i (run-sniper:246-250 -> make_hetero_config(), which transposes
# them into the `key[] = v0,v1,v2,v3` arrays sniper_config.py indexes by core id).
#
# IMPORTANT: only ONE comma-separated -c is honoured -- run-sniper *assigns*
# heteroconfig rather than appending, so a second one silently replaces the first.
# That is why each arm's L2/BTB/prefetcher values are written INTO the four per-core
# files below instead of being passed as a separate `-c core0,core1` the way the N=2
# run_cholesky_baselines.sh does.

set -eu
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
CFG_DIR=/root/benchmarks
cd "$CFG_DIR"

# ICOUNT may be overridden by the caller (run_n4_sweep.sh) to shorten runs.
ICOUNT="${ICOUNT:-1000000000}"
BENCH="${1:?usage: run_n4_hetero.sh <benchmark> <arm>}"
ARM="${2:?usage: run_n4_hetero.sh <benchmark> <arm>}"

# ---------------------------------------------------------------------------
# Core types. Both stay on nehalem's `interval` core model -- the ONLY thing that
# differs is microarchitectural width/speed. Deliberately not cortex-a72/a53: those
# would swap the core model wholesale and put the RF model's inputs even further
# out of distribution than the heterogeneity being studied.
# ---------------------------------------------------------------------------
#                        perf    eff
FREQ_PERF=2.66;     FREQ_EFF=1.20
DISPATCH_PERF=4;    DISPATCH_EFF=2
WINDOW_PERF=128;    WINDOW_EFF=32
L1D_PERF=32;        L1D_EFF=16

# Per-arm reconfigurable dimensions (L2 KB / BTB entries / prefetcher), per core type.
case "$ARM" in
  no_change|dynamic_rf)
    # Baseline sizes; dynamic_rf starts here too and lets the model move it.
    L2_PERF=256;  L2_EFF=128
    BTB_PERF=512; BTB_EFF=256
    PF_PERF=none; PF_EFF=none
    ;;
  max_resources)
    L2_PERF=1024; L2_EFF=1024
    BTB_PERF=4096; BTB_EFF=4096
    PF_PERF=simple; PF_EFF=simple
    ;;
  max_resources_nopf)
    # Same maximal caches/BTB as max_resources but prefetching OFF. Disambiguates the
    # collapse seen at both N=2 and N=4 (max_resources runs ~4.3-4.8x slower than every
    # other arm): is it the aggressive prefetcher saturating shared bandwidth, or simply
    # the larger caches? Without this arm, "dynamic_rf beats max_resources by 21x" cannot
    # be attributed, and reads as "beats a badly-configured prefetcher".
    L2_PERF=1024; L2_EFF=1024
    BTB_PERF=4096; BTB_EFF=4096
    PF_PERF=none; PF_EFF=none
    ;;
  best_static)
    # NOTE: there is no N=4 heterogeneous sweep, so this is the N=2 hindsight-optimal
    # config transplanted, NOT a hindsight optimum for this configuration. Do not
    # report savings_vs_best_static as a headline here -- see PLAN_n4_heterogeneous.md.
    L2_PERF=1024; L2_EFF=512
    BTB_PERF=4096; BTB_EFF=2048
    PF_PERF=none; PF_EFF=none
    ;;
  *) echo "unknown arm: $ARM" >&2; exit 1 ;;
esac

write_core_cfg () {   # $1=file $2=freq $3=dispatch $4=window $5=l1d $6=l2 $7=btb $8=pf
  cat > "$1" <<EOF
[perf_model/core]
frequency = $2

[perf_model/core/interval_timer]
dispatch_width = $3
window_size = $4

[perf_model/l1_dcache]
cache_size = $5

[perf_model/l2_cache]
cache_size = $6
prefetcher = $8

[perf_model/branch_predictor]
num_entries = $7
EOF
}

write_core_cfg "$CFG_DIR/hc0.cfg" $FREQ_PERF $DISPATCH_PERF $WINDOW_PERF $L1D_PERF $L2_PERF $BTB_PERF $PF_PERF
write_core_cfg "$CFG_DIR/hc1.cfg" $FREQ_PERF $DISPATCH_PERF $WINDOW_PERF $L1D_PERF $L2_PERF $BTB_PERF $PF_PERF
write_core_cfg "$CFG_DIR/hc2.cfg" $FREQ_EFF  $DISPATCH_EFF  $WINDOW_EFF  $L1D_EFF  $L2_EFF  $BTB_EFF  $PF_EFF
write_core_cfg "$CFG_DIR/hc3.cfg" $FREQ_EFF  $DISPATCH_EFF  $WINDOW_EFF  $L1D_EFF  $L2_EFF  $BTB_EFF  $PF_EFF

# dynamic_rf drives the real model through the host bridge; every other arm holds its
# starting config via noop_predict.py, which runs directly in-container.
if [ "$ARM" = "dynamic_rf" ]; then
  HOOK=$SNIPER_ROOT/.reconfig_bridge_shim.sh
else
  HOOK=$SNIPER_ROOT/tools/reconfig/noop_predict.py
fi

# RESULTS_ROOT lets a second pass (e.g. a longer --icount) write to a separate
# tree instead of colliding with, and being skipped by, the first pass.
RESULTS_ROOT="${RESULTS_ROOT:-$SNIPER_ROOT/results/n4_hetero}"
OUTDIR=$RESULTS_ROOT/$BENCH/$ARM
# McPAT never overwrites power-* files (unique t0/t1 per interval), so a re-run into a
# dirty directory silently mixes two runs' samples. Only decisions.csv self-truncates.
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

/root/benchmarks/run-sniper --benchmarks "splash2-${BENCH}-small-4" -n 4 -c gainestown \
  -c hc0,hc1,hc2,hc3 \
  -s stop-by-icount:$ICOUNT \
  -d "$OUTDIR" \
  -g perf_model/branch_predictor/num_ways=4 \
  -g perf_model/l2_cache/prefetcher/prefetch_on_prefetch_hit=true \
  -g perf_model/l2_cache/prefetcher/simple/flows=16 \
  -g perf_model/l2_cache/prefetcher/simple/num_prefetches=4 \
  -g perf_model/l2_cache/prefetcher/simple/stop_at_page_boundary=false \
  -g perf_model/l2_cache/prefetcher/simple/flows_per_core=false \
  -g general/max_instructions=$ICOUNT \
  -greconfig/enabled=true \
  -greconfig/python_hook_script=$HOOK \
  -greconfig/mcpat_script_path=$SNIPER_ROOT/tools/mcpat.py \
  -greconfig/decision_log_path="$OUTDIR/sniper_reconfig_decisions.csv" \
  -greconfig/live_config_path="$OUTDIR/sniper_reconfig_live.cfg"

echo
echo "=== $BENCH / $ARM done -> $OUTDIR ==="
echo "per-core config actually applied (check the [] arrays are 4 wide and asymmetric):"
grep -E "^(frequency|dispatch_width|window_size|cache_size|num_entries|prefetcher)" "$OUTDIR/sim.cfg" | head -20
echo "failed predictions (must be 0 for dynamic_rf):"
grep -c "predict_failed\|parse_failed" "$OUTDIR/sniper_reconfig_decisions.csv" || true
