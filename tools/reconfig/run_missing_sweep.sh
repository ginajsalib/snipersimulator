#!/bin/bash
# Run the sweep configurations that were never simulated, inside the CentOS6 container.
#
#   run_missing_sweep.sh --list <file.tsv> [--limit N] [--benchmarks "a b"] [--go]
#
# Consumes the TSV written by gen_missing_configs.py. DRY RUN BY DEFAULT -- pass --go to
# actually execute, because the full list is 3072 runs (~64 days serial).
#
# Methodology is copied from runSniperWithCfg.sh, the script that produced the existing
# merged_full_<bench>.csv, so the new rows merge with the old ones: same -c gainestown
# -c rob, same per-core cfg mechanism, same periodicins-stats/powertrace-ins scripts,
# same 1e9 instruction budget, same prefetcher tuning, and the same output directory
# naming (the extraction pipeline parses the config back out of that name).
#
# Runs strictly SEQUENTIALLY, one simulation at a time. The per-core cfg files still get
# run-specific names rather than runSniperWithCfg.sh's fixed core0.cfg/core1.cfg, so this
# cannot collide with a manual runSniperWithCfg.sh invocation happening alongside it.

set -u
export SNIPER_ROOT=${SNIPER_ROOT:-/export/sniperCodeNewBranch-centos6}
CFG_DIR=/root/benchmarks
OUT_ROOT=${OUT_ROOT:-/export}
NUM_CORES=2
DISPATCH_WIDTH=4
ICOUNT=${ICOUNT:-1000000000}

LIST=""
LIMIT=0
ONLY=""
GO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --list)       LIST="$2"; shift 2 ;;
    --limit)      LIMIT="$2"; shift 2 ;;
    --benchmarks) ONLY="$2"; shift 2 ;;
    --go)         GO=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$LIST" ] || { echo "need --list <file.tsv> (see gen_missing_configs.py)" >&2; exit 1; }
# Resolve a relative --list against $SNIPER_ROOT as well as the CWD: the lists are
# generated into $SNIPER_ROOT/results/, and this script cd's to $CFG_DIR before running
# anything, so a bare "./results/..." would otherwise resolve somewhere unhelpful.
if [ ! -f "$LIST" ] && [ -f "$SNIPER_ROOT/$LIST" ]; then
  LIST="$SNIPER_ROOT/$LIST"
elif [ ! -f "$LIST" ] && [ -f "$SNIPER_ROOT/results/$(basename "$LIST")" ]; then
  LIST="$SNIPER_ROOT/results/$(basename "$LIST")"
fi
if [ ! -f "$LIST" ]; then
  echo "no such list: $LIST" >&2
  echo "available under $SNIPER_ROOT/results/:" >&2
  ls "$SNIPER_ROOT/results/"*.tsv 2>/dev/null >&2 || echo "  (none -- run gen_missing_configs.py on the host first)" >&2
  exit 1
fi
echo "list       : $LIST"

cd "$CFG_DIR"
STATUS=$OUT_ROOT/missing_sweep_status.csv
mkdir -p "$OUT_ROOT"
[ -f "$STATUS" ] || echo "benchmark,l2c0,l2c1,l3,pf0,pf1,btb0,btb1,outcome,dir" > "$STATUS"

run_one () {   # $1..$8 = bench l2c0 l2c1 l3 pf0 pf1 btb0 btb1
  local bench=$1 l20=$2 l21=$3 l3=$4 pf0=$5 pf1=$6 bp0=$7 bp1=$8
  local dir="$OUT_ROOT/config_l2_${l20}_${l21}_l3MB_${l3}_prefetch_${pf0}_${pf1}_branch_${bp0}-${bp1}_${bench}-intervals"

  # sim.out only exists on a clean exit, so it is the completion marker (a crashed run
  # still leaves periodic stats behind -- see the no_change segfault).
  if [ -f "$dir/sim.out" ]; then
    echo "SKIP  $bench l2=$l20/$l21 l3=$l3 pf=$pf0/$pf1 btb=$bp0/$bp1 (done)"
    return 0
  fi

  # Distinct from runSniperWithCfg.sh's fixed core0.cfg/core1.cfg so the two cannot
  # interfere if both are ever used against the same CFG_DIR.
  local tag="${bench}_${l20}_${l21}_${l3}_${pf0}_${pf1}_${bp0}_${bp1}"
  local c0="$CFG_DIR/mc0_${tag}.cfg" c1="$CFG_DIR/mc1_${tag}.cfg"
  cat > "$c0" <<EOF
[perf_model/core/interval_timer]
dispatch_width = ${DISPATCH_WIDTH}

[perf_model/branch_predictor]
num_entries = ${bp0}

[perf_model/l2_cache]
prefetcher = ${pf0}
cache_size = ${l20}
EOF
  cat > "$c1" <<EOF
[perf_model/core/interval_timer]
dispatch_width = ${DISPATCH_WIDTH}

[perf_model/branch_predictor]
num_entries = ${bp1}

[perf_model/l2_cache]
prefetcher = ${pf1}
cache_size = ${l21}
EOF

  mkdir -p "$dir"
  /root/benchmarks/run-sniper \
    --benchmarks "splash2-${bench}-small-4" \
    -n "$NUM_CORES" \
    -c gainestown -c rob \
    -c "$(basename "$c0" .cfg),$(basename "$c1" .cfg)" \
    -s "periodicins-stats.py:500000" \
    -s "powertrace-ins.py" \
    -s "stop-by-icount:${ICOUNT}" \
    -d "$dir" \
    -g "perf_model/l3_cache/cache_size=${l3}" \
    -g "perf_model/branch_predictor/num_ways=4" \
    -g "general/max_instructions=${ICOUNT}" \
    -g "perf_model/l2_cache/prefetcher/prefetch_on_prefetch_hit=true" \
    -g "perf_model/l2_cache/prefetcher/simple/flows=16" \
    -g "perf_model/l2_cache/prefetcher/simple/num_prefetches=4" \
    -g "perf_model/l2_cache/prefetcher/simple/stop_at_page_boundary=false" \
    -g "perf_model/l2_cache/prefetcher/simple/flows_per_core=false" \
    > "$dir/run.log" 2>&1
  local rc=$?
  rm -f "$c0" "$c1"

  if [ -f "$dir/sim.out" ]; then
    echo "$bench,$l20,$l21,$l3,$pf0,$pf1,$bp0,$bp1,ok,$dir" >> "$STATUS"
    echo "OK    $bench l2=$l20/$l21 l3=$l3 pf=$pf0/$pf1 btb=$bp0/$bp1"
  else
    echo "$bench,$l20,$l21,$l3,$pf0,$pf1,$bp0,$bp1,failed,$dir" >> "$STATUS"
    echo "FAIL  $bench l2=$l20/$l21 l3=$l3 pf=$pf0/$pf1 btb=$bp0/$bp1 (rc=$rc, see $dir/run.log)"
  fi
}

# ---- build the work list -------------------------------------------------------
WORK=$(mktemp)
grep -v '^#' "$LIST" | while IFS=$'\t' read -r b a c d e f g h; do
  [ -n "${b:-}" ] || continue
  if [ -n "$ONLY" ]; then
    case " $ONLY " in *" $b "*) ;; *) continue ;; esac
  fi
  dir="$OUT_ROOT/config_l2_${a}_${c}_l3MB_${d}_prefetch_${e}_${f}_branch_${g}-${h}_${b}-intervals"
  [ -f "$dir/sim.out" ] && continue        # already complete
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$b" "$a" "$c" "$d" "$e" "$f" "$g" "$h"
done > "$WORK"
[ "$LIMIT" -gt 0 ] && { head -n "$LIMIT" "$WORK" > "$WORK.cut"; mv "$WORK.cut" "$WORK"; }

TOTAL=$(wc -l < "$WORK")
echo "runs to do : $TOTAL   (already-complete ones skipped)"
echo "icount     : $ICOUNT"
echo "output     : $OUT_ROOT/config_l2_*"
awk -F'\t' '{print $1}' "$WORK" | sort | uniq -c | awk '{printf "   %-12s %d\n", $2, $1}'
echo "est. wall  : ~$(awk -v t="$TOTAL" 'BEGIN{printf "%.1f", t*30/1440.0}') days (sequential, at ~30 min/run)"

if [ "$GO" != "1" ]; then
  echo
  echo "DRY RUN -- nothing executed. Re-run with --go to start."
  echo "Consider --benchmarks / --limit first: the full list is ~64 days."
  rm -f "$WORK"
  exit 0
fi

# ---- execute (sequential, one simulation at a time) -----------------------------
done_n=0
while IFS=$'\t' read -r b a c d e f g h; do
  done_n=$((done_n+1))
  echo
  echo "[$done_n/$TOTAL] $(date '+%F %T')"
  run_one "$b" "$a" "$c" "$d" "$e" "$f" "$g" "$h"
done < "$WORK"
rm -f "$WORK"

echo
echo "=================== missing-sweep pass complete ==================="
awk -F, 'NR>1{c[$9]++} END{for(k in c) printf "  %-8s %d\n", k, c[k]}' "$STATUS"
