#!/bin/bash
# Extract newly-simulated sweep runs and APPEND them to the existing
# merged_full_<bench>.csv -- never replacing it.
#
#   extract_and_append.sh --benchmark <name> [--dirs-list <file>] [--dry-run]
#
# TWO PHASES, because the work is split across the container boundary:
#
#   phase 1 (CONTAINER) -- processIntervals.py shells out to
#       /root/sniper/tools/dumpstats.py and walks /export, both container-only paths,
#       so the per-interval performance counters must be collected there. Output lands
#       in the shared mount.
#   phase 2 (HOST)      -- McPAT power parsing and the pandas pipeline stages, where the
#       large CSVs live.
#
# Run this script in either place: it detects which side it is on and does that phase,
# then tells you the command for the other.
#
# The stock pipeline (run_pipeline.py) regenerates merged_full_<bench>.csv from scratch
# and would overwrite years of collected data, so this drives the same Stage-1 scripts
# by hand into temporary files and then appends only genuinely new rows, keyed on
# (period_start, L2 core 0, L2 core 1, L3, Prefetch core 0, Prefetch core 1,
# BTB core 0, BTB core 1). Re-running is therefore safe and idempotent.
#
# A timestamped backup of merged_full_<bench>.csv is taken before any append.

set -eu
PYS=/home/gina/Desktop/snipersim_framework/pythonScripts
FRAMEWORK=/home/gina/Desktop/snipersim_framework
MOUNT=/home/gina/Desktop/dockerMnt
MERGED_DIR=$PYS/merged_full

BENCH=""
DIRS_LIST=""
DRY=0
# $SNIPER_ROOT/results inside the container == $MOUNT/sniperCodeNewBranch-centos6/results
# on the host; the perf CSV is handed between the phases through it.
CONTAINER_SNIPER_ROOT=/export/sniperCodeNewBranch-centos6
while [ $# -gt 0 ]; do
  case "$1" in
    --benchmark) BENCH="$2"; shift 2 ;;
    --dirs-list) DIRS_LIST="$2"; shift 2 ;;
    --dry-run)   DRY=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$BENCH" ] || { echo "need --benchmark <name>" >&2; exit 1; }

PERF_SHARED_REL=results/extract_${BENCH}_perf.csv

# ---------------- phase 1: container ----------------
if [ -d /export ] && [ -f /root/sniper/tools/dumpstats.py ]; then
  OUT=$CONTAINER_SNIPER_ROOT/$PERF_SHARED_REL
  mkdir -p "$(dirname "$OUT")"
  ONLY=$(mktemp)
  if [ -n "$DIRS_LIST" ]; then
    cp "$DIRS_LIST" "$ONLY"
  else
    ls -d /export/config_l2_*_"${BENCH}"-intervals 2>/dev/null | xargs -r -n1 basename > "$ONLY" || true
  fi
  echo "phase 1 (container): collecting per-interval counters for $BENCH"
  echo "  directories: $(wc -l < "$ONLY")"
  echo "  output     : $OUT"
  # processIntervals.py lives in snipersim_framework, which is NOT under the bind mount,
  # so a copy is kept alongside this script. And NOT "python3": that is the
  # .reconfig_bridge_shim.sh symlink in this container -- use the real interpreter.
  PY3=/opt/rh/rh-python36/root/usr/bin/python3.6
  [ -x "$PY3" ] || PY3=/opt/python3.11/bin/python3.11
  "$PY3" "$CONTAINER_SNIPER_ROOT/tools/reconfig/processIntervals.py" "$BENCH" "$OUT" "$ONLY"
  rm -f "$ONLY"
  echo
  echo "phase 1 done. Now on the HOST:"
  echo "  bash tools/reconfig/extract_and_append.sh --benchmark $BENCH --dry-run"
  exit 0
fi

# ---------------- phase 2: host ----------------
TARGET=$MERGED_DIR/merged_full_${BENCH}.csv
[ -f "$TARGET" ] || { echo "no existing $TARGET to append to" >&2; exit 1; }
PERF_FROM_CONTAINER=$MOUNT/sniperCodeNewBranch-centos6/$PERF_SHARED_REL
if [ ! -f "$PERF_FROM_CONTAINER" ]; then
  echo "missing $PERF_FROM_CONTAINER" >&2
  echo "Run phase 1 inside the container first:" >&2
  echo "  bash \$SNIPER_ROOT/tools/reconfig/extract_and_append.sh --benchmark $BENCH" >&2
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "benchmark : $BENCH"
echo "target    : $TARGET  ($(wc -l < "$TARGET") lines)"
echo "workdir   : $WORK"

ls -d "$MOUNT"/config_l2_*_"${BENCH}"-intervals 2>/dev/null | xargs -r -n1 basename > "$WORK/only.txt" || true
echo "dirs      : $(wc -l < "$WORK/only.txt") on disk for $BENCH"

echo
echo "--- stage 0a: per-interval counters (from container phase) ---"
cp "$PERF_FROM_CONTAINER" "$WORK/perf.csv"
echo "  $(wc -l < "$WORK/perf.csv") rows from $PERF_FROM_CONTAINER"

echo
echo "--- stage 0b: per-interval McPAT power ---"
( cd "$FRAMEWORK" && python3 collect_all_power.py "$MOUNT" "$WORK/power.csv" "$BENCH" "$WORK/only.txt" ) \
  | tail -3

echo
echo "--- stage 1: parse config out of directory name, clean, merge perf+power, derive ---"
cd "$PYS"
python3 parseInputConfigsOnPerf.py        "$WORK/perf.csv"    "$WORK/parsed.csv"
python3 deleteEmptyRows.py                "$WORK/parsed.csv"  "$WORK/clean.csv"
python3 mergePerfAndPower.py              "$WORK/clean.csv"   "$WORK/power.csv" "$WORK/merged.csv"
python3 addCalculatedColumnsToMergedCsv.py "$WORK/merged.csv" "$WORK/new_full.csv"
# Stage scripts have historically printed an error and returned normally, so verify the
# artefact exists rather than trusting the exit status.
for f in parsed.csv clean.csv merged.csv new_full.csv; do
  [ -s "$WORK/$f" ] || { echo "stage 1 produced no $f -- see the output above" >&2; exit 1; }
done

echo
echo "--- stage 2: append only rows not already present ---"
python3 - "$TARGET" "$WORK/new_full.csv" "$DRY" <<'PY'
import sys, pandas as pd, shutil, datetime, os
target, new, dry = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
KEY = ['period_start','L2 core 0','L2 core 1','L3',
       'Prefetch core 0','Prefetch core 1','BTB core 0','BTB core 1']

newdf = pd.read_csv(new, low_memory=False)
missing = [k for k in KEY if k not in newdf.columns]
if missing:
    sys.exit('new data lacks key columns: %s' % missing)

# Stream the (large) target to collect existing keys and verify the schema matches,
# rather than loading the whole file into memory.
existing, cols = set(), None
for chunk in pd.read_csv(target, usecols=None, chunksize=200000, low_memory=False):
    if cols is None:
        cols = list(chunk.columns)
        miss = [k for k in KEY if k not in cols]
        if miss:
            sys.exit('target lacks key columns: %s' % miss)
    existing.update(map(tuple, chunk[KEY].astype(str).values.tolist()))

only_new = [c for c in newdf.columns if c not in cols]
only_old = [c for c in cols if c not in newdf.columns]
print('  target rows (keys): %d' % len(existing))
print('  new rows          : %d' % len(newdf))
if only_new: print('  WARNING columns only in new data (will be dropped): %s' % only_new[:8])
if only_old: print('  WARNING columns only in target (blank in new rows): %s' % only_old[:8])

mask = ~pd.Series(list(map(tuple, newdf[KEY].astype(str).values.tolist()))).isin(existing)
add = newdf[mask.values]
print('  already present   : %d' % (len(newdf) - len(add)))
print('  TO APPEND         : %d' % len(add))
if dry:
    print('  (dry run -- nothing written)'); sys.exit(0)
if add.empty:
    print('  nothing to do'); sys.exit(0)

bak = target + '.bak-' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
shutil.copy2(target, bak)
print('  backup            : %s' % bak)
# reindex to the target's exact column order so the appended block lines up
add = add.reindex(columns=cols)
add.to_csv(target, mode='a', header=False, index=False)
print('  appended %d rows -> %s' % (len(add), target))
PY
