#!/bin/bash
# Archive completed sweep run directories to free disk, the same way the earlier
# benchmarks' runs were archived.
#
#   archive_sweep_runs.sh --benchmark <name> [--dest <dir>] [--delete] [--dry-run]
#
# Each directory becomes its own <name>.tar.zst (~12x smaller: 188 MB -> 15 MB on a
# sample radiosity run), so archiving and deletion can proceed incrementally and disk
# usage falls monotonically rather than needing headroom for one giant archive.
#
# Deletion is OPT-IN (--delete) and only happens after the archive is verified twice:
# `zstd -t` for integrity, and a file-count comparison against the original directory.
# Without --delete this only adds the archives, which temporarily *increases* usage.
#
# Only directories containing sim.out are touched -- an incomplete run is not worth
# archiving, and deleting one would discard a run that may just need resuming.

set -u
# Must usually run INSIDE the container: the run directories are owned by root (the
# container writes them), so a host-side rm gets "Permission denied". The mount is
# /export there and ~/Desktop/dockerMnt on the host, so default to whichever exists.
if [ -z "${MOUNT:-}" ]; then
  if [ -d /export ]; then MOUNT=/export; else MOUNT=/home/gina/Desktop/dockerMnt; fi
fi
BENCH=""
DEST=""
DELETE=0
DRY=0
BEFORE=""
# Run directories are owned by root (written by the container), so a host-side delete
# needs --sudo. Reading them for the tar does not: the files are world-readable.
SUDO=
while [ $# -gt 0 ]; do
  case "$1" in
    --benchmark) BENCH="$2"; shift 2 ;;
    --dest)      DEST="$2";  shift 2 ;;
    --delete)    DELETE=1;   shift ;;
    --before)    BEFORE="$2"; shift 2 ;;
    --sudo)      SUDO=sudo;   shift ;;
    --dry-run)   DRY=1;      shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
[ -n "$BENCH" ] || { echo "need --benchmark <name>" >&2; exit 1; }
DEST=${DEST:-$MOUNT/archive_${BENCH}}
mkdir -p "$DEST"

# zstd is not present on CentOS 6; fall back through pigz to plain gzip. Ratio drops
# (roughly 10x -> 6-7x) but the win is still large, and mixing extensions across runs
# is harmless since each archive records its own.
if command -v zstd >/dev/null 2>&1;   then COMP="zstd -3 -T0 -q -o"; EXT="tar.zst"; TARLIST="tar -I zstd -tf"
elif command -v pigz >/dev/null 2>&1; then COMP="pigz -c >";          EXT="tar.gz";  TARLIST="tar -tzf"
else                                       COMP="gzip -c >";          EXT="tar.gz";  TARLIST="tar -tzf"
fi
echo "compressor: $(echo "$COMP" | awk '{print $1}')  (.$EXT)"

# --before protects runs that are still being produced, or that completed after the
# last extraction and are therefore not yet in merged_full.
# An array, not a string: the timestamp contains a space, so an unquoted
# "-not -newermt $BEFORE" word-splits and find reports
# "paths must precede expression: 13:00".
BEFORE_ARGS=()
[ -n "$BEFORE" ] && BEFORE_ARGS=(-not -newermt "$BEFORE")
mapfile -t DIRS < <(find "$MOUNT" -maxdepth 1 -type d -name "config_l2_*_${BENCH}-intervals" \
                      ${BEFORE_ARGS[@]+"${BEFORE_ARGS[@]}"} -exec test -f {}/sim.out \; -print | sort)
[ -n "$BEFORE" ] && echo "filter    : only directories older than $BEFORE"
echo "benchmark : $BENCH"
echo "complete  : ${#DIRS[@]} directories"
echo "dest      : $DEST"
echo "delete    : $([ "$DELETE" = 1 ] && echo "YES (after verification)${SUDO:+, via sudo}" || echo 'no (archives only)')"
echo "disk now  : $(df -h "$MOUNT" | tail -1 | awk '{print $4" free ("$5" used)"}')"
[ "${#DIRS[@]}" -gt 0 ] || { echo "nothing to do"; exit 0; }
if [ "$DRY" = "1" ]; then echo; echo "DRY RUN -- nothing written."; exit 0; fi

n=0; ok=0; failed=0; skipped=0
for d in "${DIRS[@]}"; do
  n=$((n+1))
  base=$(basename "$d")
  arc="$DEST/${base}.${EXT}"
  printf "[%d/%d] %s " "$n" "${#DIRS[@]}" "${base:0:62}"

  if [ -f "$arc" ]; then
    printf "(archive exists) "
  else
    if ! eval "tar -C \"$MOUNT\" -cf - \"$base\" 2>/dev/null | $COMP \"$arc\"" 2>/dev/null; then
      echo "ARCHIVE FAILED"; rm -f "$arc"; failed=$((failed+1)); continue
    fi
  fi

  # verify: integrity, then that the archive holds as many entries as the directory
  if ! $TARLIST "$arc" >/dev/null 2>&1; then
    echo "CORRUPT ARCHIVE"; rm -f "$arc"; failed=$((failed+1)); continue
  fi
  want=$(find "$d" -type f | wc -l)
  got=$($TARLIST "$arc" 2>/dev/null | grep -vc '/$')
  if [ "$got" -lt "$want" ]; then
    echo "INCOMPLETE ($got of $want files)"; failed=$((failed+1)); continue
  fi

  if [ "$DELETE" = "1" ]; then
    $SUDO rm -rf "$d" && printf "archived+removed"
  else
    printf "archived"
  fi
  echo " ($(du -sm "$arc" | awk '{print $1}') MB)"
  ok=$((ok+1))
done

echo
echo "=== done: $ok archived, $failed failed, $skipped skipped ==="
echo "disk now  : $(df -h "$MOUNT" | tail -1 | awk '{print $4" free ("$5" used)"}')"
[ "$DELETE" = "1" ] || echo "Originals kept. Re-run with --delete to reclaim the space."
