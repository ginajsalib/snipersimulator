#!/bin/bash
# A/B the two shrink policies on the same benchmark(s), N=4 heterogeneous.
# Run inside the CentOS6 container, from /root/benchmarks.
#
#   run_shrink_policy_ab.sh [--benchmarks "a b c"] [--input small|large] [--icount N]
#
# Only reconfig/shrink_policy differs between the two arms:
#   clamp  -- a shrink that doesn't fit is refused (Cache::setActiveWays() raises it to
#             the highest occupied way). Never evicts. On a warm cache one fully-occupied
#             set pins the whole cache, so shrinks are near-permanent no-ops.
#   flush  -- the ways being gated are evicted first, so the shrink actually takes effect,
#             at the cost of real writeback / back-invalidation traffic.
#
# Only dynamic_rf is run: the static arms never request a shrink, so the policy cannot
# affect them and running them twice would waste hours for identical output.
#
# SERIAL BY NECESSITY -- ReconfigurationManager hardcodes /tmp/sniper_interval_stats.json
# and /tmp/sniper_new_config.json, so two reconfig-enabled runs in one container would
# read each other's files.

set -u
export SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
cd /root/benchmarks

BENCHMARKS="cholesky"
INPUT=small
ICOUNT=1000000000
while [ $# -gt 0 ]; do
  case "$1" in
    --benchmarks) BENCHMARKS="$2"; shift 2 ;;
    --input)      INPUT="$2";      shift 2 ;;
    --icount)     ICOUNT="$2";     shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

ROOT=$SNIPER_ROOT/results/shrink_policy_ab
mkdir -p "$ROOT"
STATUS=$ROOT/status.csv
[ -f "$STATUS" ] || echo "benchmark,policy,input,outcome,intervals,transitions,flush_lines,flush_dirty" > "$STATUS"

for bench in $BENCHMARKS; do
  for policy in clamp flush; do
    OUTDIR=$ROOT/$bench/$policy
    if [ -f "$OUTDIR/sim.out" ]; then
      echo "== SKIP $bench/$policy (already complete) =="
      continue
    fi
    echo
    echo "======================================================================"
    echo "== $bench  shrink_policy=$policy  input=$INPUT   ($(date '+%F %T'))"
    echo "======================================================================"

    # Same per-core heterogeneous config and same naive-max starting point as
    # run_n4_hetero.sh's dynamic_rf arm, so the only difference is the policy.
    RESULTS_ROOT=$ROOT/.tmp_$policy \
    ICOUNT=$ICOUNT INPUT=$INPUT \
    SHRINK_POLICY=$policy \
      bash "$SNIPER_ROOT/tools/reconfig/run_n4_hetero.sh" "$bench" dynamic_rf 2>&1 \
      | tee "$ROOT/.log_${bench}_${policy}.txt"

    rm -rf "$OUTDIR"; mkdir -p "$(dirname "$OUTDIR")"
    mv "$ROOT/.tmp_$policy/$bench/dynamic_rf" "$OUTDIR" 2>/dev/null
    rm -rf "$ROOT/.tmp_$policy"
    mv "$ROOT/.log_${bench}_${policy}.txt" "$OUTDIR/run.log" 2>/dev/null

    if [ -f "$OUTDIR/sim.out" ]; then
      n=$(ls "$OUTDIR"/power-*.txt 2>/dev/null | wc -l)
      # flush volume is printed per flush by CacheCntlr::reconfigure()
      fl=$(grep -o "flush-shrink .*: [0-9]* lines" "$OUTDIR/run.log" 2>/dev/null \
           | grep -o "[0-9]* lines" | grep -o "[0-9]*" | paste -sd+ | bc 2>/dev/null)
      fd=$(grep -o "([0-9]* dirty)" "$OUTDIR/run.log" 2>/dev/null \
           | grep -o "[0-9]*" | paste -sd+ | bc 2>/dev/null)
      tr=$(python3 - "$OUTDIR/sniper_reconfig_decisions.csv" <<'PY' 2>/dev/null
import csv,sys
rows=list(csv.DictReader(open(sys.argv[1])))
prev=[c for c in (rows[0] if rows else {}) if '_prev' in c]
n=0; last=None
for r in rows:
    cur=tuple(r[c] for c in prev)
    if last is not None and cur!=last: n+=1
    last=cur
print(n)
PY
)
      echo "$bench,$policy,$INPUT,ok,$n,${tr:-?},${fl:-0},${fd:-0}" >> "$STATUS"
      echo "-> ok: $n intervals, ${tr:-?} transitions, ${fl:-0} lines flushed (${fd:-0} dirty)"
    else
      echo "$bench,$policy,$INPUT,failed,0,,," >> "$STATUS"
      echo "-> FAILED (no sim.out)"
    fi
  done
done

echo
echo "=================== A/B complete ==================="
column -s, -t "$STATUS" 2>/dev/null || cat "$STATUS"
echo
echo "PPW comparison -- run on the HOST:"
echo "  python2 tools/reconfig/analyze_final_experiment.py shrink-ab \\"
echo "    --root /home/gina/Desktop/dockerMnt/sniperCodeNewBranch-centos6/results/shrink_policy_ab"
