#!/bin/bash
# Run inside the CentOS6 container to isolate why noop_predict.py exited nonzero
# (every interval of the drift diagnostic came back "predict_failed"). Checks the
# rebuild landed, then invokes noop_predict.py directly (bypassing run-sniper/
# system()) so its actual stderr/traceback and exit code are visible unfiltered.

set -u
SNIPER_ROOT="${SNIPER_ROOT:-/export/sniperCodeNewBranch-centos6}"

echo "== rebuild sanity =="
ls -la "$SNIPER_ROOT"/lib/sniper 2>&1
ls -la "$SNIPER_ROOT"/common/core/memory_subsystem/parametric_dram_directory_msi/cache_cntlr.o 2>&1
grep -q "reconfig-debug" "$SNIPER_ROOT"/common/core/memory_subsystem/parametric_dram_directory_msi/cache_cntlr.cc \
  && echo "[ok] debug print present in source" \
  || echo "[FAIL] debug print missing -- dockerMnt sync didn't land"

echo
echo "== which python3, and can it write /tmp =="
which python3
python3 -c "print('python3 runs fine')"
touch /tmp/_write_test && echo "[ok] /tmp is writable" && rm -f /tmp/_write_test

echo
echo "== does a stats file exist from the failed run =="
ls -la /tmp/sniper_interval_stats.json 2>&1

echo
echo "== invoking noop_predict.py directly (real stderr, real exit code) =="
python3 "$SNIPER_ROOT/tools/reconfig/noop_predict.py"
echo "exit code: $?"

echo
echo "== if the stats file was missing above, synthesize one and retry =="
if [ ! -s /tmp/sniper_interval_stats.json ]; then
  cat > /tmp/sniper_interval_stats.json <<'EOF'
{
  "cores": [
    {"core_id": 0, "ipc": 0.5, "l1_miss_rate": 0.01, "l2_miss_rate": 0.01, "l3_miss_rate": 0.01, "branch_mpki": 2.0, "l2_prev": 1048576, "btb_prev": 4096, "prefetcher_prev": "simple"},
    {"core_id": 1, "ipc": 0.0, "l1_miss_rate": 0.0, "l2_miss_rate": 0.0, "l3_miss_rate": 0.0, "branch_mpki": 0.0, "l2_prev": 1048576, "btb_prev": 4096, "prefetcher_prev": "simple"}
  ],
  "l3": {"l3_prev": 16777216},
  "active_cores": 2
}
EOF
  python3 "$SNIPER_ROOT/tools/reconfig/noop_predict.py"
  echo "exit code with synthesized stats: $?"
  cat /tmp/sniper_new_config.json 2>&1
fi
