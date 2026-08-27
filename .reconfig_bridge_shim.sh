#!/bin/bash
# Stands in for "python3" for the single command ReconfigurationManager::runPythonPrediction()
# runs: `python3 tools/reconfig/rf_predict.py`. Instead of actually running Python here (this
# old container can't run a Python new enough for the real model), it hands the stats file to
# a host-side watcher through the already-mounted project directory and waits for the answer.
# See .reconfig_bridge_watch.py (run on the HOST, not in this container) for the other half.

BRIDGE=/export/sniperCodeNewBranch-centos6/.reconfig_bridge
mkdir -p "$BRIDGE"

cp /tmp/sniper_interval_stats.json "$BRIDGE/stats_request.json" || exit 1
rm -f "$BRIDGE/config_response.json" "$BRIDGE/config_response.ready"
touch "$BRIDGE/stats_request.ready"

# Poll for the host's response. 300 * 0.1s = 30s timeout.
for i in $(seq 1 300); do
  if [ -f "$BRIDGE/config_response.ready" ]; then
    cp "$BRIDGE/config_response.json" /tmp/sniper_new_config.json
    rm -f "$BRIDGE/stats_request.ready" "$BRIDGE/config_response.ready"
    exit 0
  fi
  sleep 0.1
done

echo "[reconfig bridge] timed out waiting for host-side predictor (is .reconfig_bridge_watch.py running on the host?)" >&2
rm -f "$BRIDGE/stats_request.ready"
exit 1
