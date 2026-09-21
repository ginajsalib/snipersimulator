#!/bin/bash
# Orchestrate the N=4 heterogeneous experiment across every SPLASH-2 benchmark.
# Run inside the CentOS6 container, from /root/benchmarks.
#
#   run_n4_sweep.sh [--arms "a b c"] [--benchmarks "x y z"] [--icount N] [--dry-run]
#
# Skips benchmarks that fail to run (some SPLASH programs reject 4 threads, or have no
# 'small' input) and keeps going -- the outcome of every (benchmark, arm) pair is
# appended to results/n4_hetero/sweep_status.csv so the run is RESUMABLE: re-running
# skips any pair already recorded as ok.
#
# SERIAL BY NECESSITY. ReconfigurationManager hardcodes /tmp/sniper_interval_stats.json
# and /tmp/sniper_new_config.json (reconfiguration_manager.cc:28-29 -- unlike
# decision_log_path/live_config_path, these are NOT config-settable). Two reconfig-enabled
# runs in the same container would read each other's files. Do not background these.

set -u
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
cd /root/benchmarks

# best_static deliberately NOT a default: it only exists for the 4 sweep
# benchmarks (barnes/cholesky/fft/radiosity) and even there it is a 2-core,
# `-c rob` config transplanted into a 4-core heterogeneous run. Add it
# explicitly with --arms if you want it for those four.
ARMS="dynamic_rf max_resources max_resources_nopf"
BENCHMARKS=""
ICOUNT=1000000000
DRYRUN=0
INPUT=small
TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arms) ARMS="$2"; shift 2 ;;
    --benchmarks) BENCHMARKS="$2"; shift 2 ;;
    --icount) ICOUNT="$2"; shift 2 ;;
    --input) INPUT="$2"; shift 2 ;;
    --tag) TAG="_$2"; shift 2 ;;
    --dry-run) DRYRUN=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

# Default: every program splashrun knows about, minus variants that are not distinct
# workloads for our purposes -- the fft_O0..O3/fft_rep2/fft_forever compiler-flag and
# repeat variants, raytrace_opt, and the *-scale variants (different input scaling, not a
# different benchmark).
if [ -z "$BENCHMARKS" ]; then
  # Ordered cheapest-first (kernels before full apps) so that incremental results
  # arrive as early as possible: if the sweep is stopped or interrupted part-way you
  # still have the most benchmarks completed, rather than a few expensive ones.
  BENCHMARKS="radix fft lu.cont lu.ncont cholesky barnes fmm radiosity water.nsq water.sp ocean.cont ocean.ncont raytrace volrend"
fi

RESULTS_ROOT=$SNIPER_ROOT/results/n4_hetero$TAG
export RESULTS_ROOT
STATUS=$RESULTS_ROOT/sweep_status.csv
mkdir -p "$(dirname "$STATUS")"
[ -f "$STATUS" ] || echo "benchmark,arm,input,outcome,intervals,note" > "$STATUS"

already_ok () {  # $1=bench $2=arm -- already recorded as ok?
  grep -qE "^$1,$2,[^,]*,ok," "$STATUS" 2>/dev/null
}

echo "benchmarks: $BENCHMARKS"
echo "arms      : $ARMS"
echo "icount    : $ICOUNT"
echo "input     : $INPUT"
echo "results   : $RESULTS_ROOT"
[ "$DRYRUN" = "1" ] && { echo "(dry run -- nothing executed)"; exit 0; }

for bench in $BENCHMARKS; do
  for arm in $ARMS; do
    if already_ok "$bench" "$arm"; then
      echo "== SKIP $bench/$arm (already ok) =="
      continue
    fi
    echo
    echo "======================================================================"
    echo "== $bench / $arm   ($(date '+%F %T'))"
    echo "======================================================================"

    if ICOUNT=$ICOUNT INPUT=$INPUT bash "$SNIPER_ROOT/tools/reconfig/run_n4_hetero.sh" "$bench" "$arm"; then
      d=$RESULTS_ROOT/$bench/$arm
      n=$(ls "$d"/power-*.txt 2>/dev/null | wc -l)
      # A run that segfaults still leaves power files behind, so completion is judged by
      # sim.out existing (written only on a clean exit) -- see the no_change crash, which
      # produced 179 power files but no sim.out and no roi-end marker.
      if [ -f "$d/sim.out" ]; then
        echo "$bench,$arm,$INPUT,ok,$n," >> "$STATUS"
        echo "-> ok ($n intervals)"
      else
        echo "$bench,$arm,$INPUT,crashed,$n,no sim.out (check debug_backtrace.out)" >> "$STATUS"
        echo "-> CRASHED after $n intervals, continuing"
      fi
    else
      echo "$bench,$arm,$INPUT,failed,0,run-sniper returned nonzero" >> "$STATUS"
      echo "-> FAILED to run, skipping"
    fi
  done

  # --- benchmark complete: its arms can be analysed NOW, without waiting for the rest.
  # The results tree is the shared bind mount, and the host-side analysis only reads,
  # so it is safe to run concurrently with the sweep still in progress.
  echo
  echo "######################################################################"
  echo "## $bench COMPLETE -- analysable now ($(date '+%F %T'))"
  echo "##   on the HOST:"
  echo "##     python2 tools/reconfig/analyze_final_experiment.py sweep \\"
  echo "##       --root /home/gina/Desktop/dockerMnt/sniperCodeNewBranch-centos6/results/n4_hetero$TAG"
  echo "######################################################################"
  grep "^$bench," "$STATUS" || true
done

echo
echo "=================== sweep complete ==================="
column -s, -t "$STATUS" 2>/dev/null || cat "$STATUS"
