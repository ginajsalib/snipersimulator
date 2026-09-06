#!/usr/bin/env python3
"""
Host-side watcher for the CentOS 6 container's reconfig bridge.

The container's "python3" (see .reconfig_bridge_shim.sh) can't run the real RF model
(old glibc has no compatible sklearn wheel, and CentOS 6's python3.6 can't run one new
enough to unpickle a model saved with sklearn 1.9.0 either). Instead of fighting that,
this script runs the unmodified predictor script on the HOST -- which already has a
close-enough sklearn -- using a request/response handoff through a directory that's
already bind-mounted into the container, so no extra container restart/mount is needed.

Which predictor script runs is set by RECONFIG_PREDICT_SCRIPT (default:
tools/reconfig/rf_predict.py, the fixed-2-core predictor). Point it at
tools/reconfig/rf_predict_ncore.py instead to get real per-core predictions for however
many cores are actually simulated, once an ncore_* model bundle exists in
tools/reconfig/model/ -- e.g.:
  RECONFIG_PREDICT_SCRIPT=tools/reconfig/rf_predict_ncore.py python3 .reconfig_bridge_watch.py
A relative path is resolved against this script's own directory, same as the default.

Protocol (all paths under BRIDGE):
  stats_request.json + stats_request.ready  -> written by the container shim
  config_response.json + config_response.ready -> written by this watcher
"""

import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, ".reconfig_bridge")

_predict_script = os.environ.get("RECONFIG_PREDICT_SCRIPT", os.path.join("tools", "reconfig", "rf_predict.py"))
RF_PREDICT = _predict_script if os.path.isabs(_predict_script) else os.path.join(HERE, _predict_script)

REQ_READY = os.path.join(BRIDGE, "stats_request.ready")
REQ_FILE = os.path.join(BRIDGE, "stats_request.json")
RESP_FILE = os.path.join(BRIDGE, "config_response.json")
RESP_READY = os.path.join(BRIDGE, "config_response.ready")

HOST_STATS_FILE = "/tmp/sniper_interval_stats.json"
HOST_CONFIG_FILE = "/tmp/sniper_new_config.json"


def main():
    os.makedirs(BRIDGE, exist_ok=True)
    print(f"[bridge] watching {BRIDGE}  (Ctrl-C to stop)", flush=True)
    print(f"[bridge] predictor script: {RF_PREDICT}", flush=True)

    while True:
        if os.path.exists(REQ_READY):
            try:
                shutil.copy(REQ_FILE, HOST_STATS_FILE)
                if os.path.exists(HOST_CONFIG_FILE):
                    os.remove(HOST_CONFIG_FILE)

                rc = subprocess.call([sys.executable, RF_PREDICT])

                if rc == 0 and os.path.exists(HOST_CONFIG_FILE):
                    shutil.copy(HOST_CONFIG_FILE, RESP_FILE)
                    open(RESP_READY, "w").close()
                    print(f"[bridge] predicted config delivered (rc={rc})", flush=True)
                else:
                    # Deliberately do NOT touch RESP_READY here: the container shim's poll
                    # loop should genuinely time out (and the C++ side skip this interval),
                    # not find a "ready" flag with no config_response.json behind it.
                    print(f"[bridge] {os.path.basename(RF_PREDICT)} failed (rc={rc}); leaving container to time out", flush=True)
            except Exception as e:
                print(f"[bridge] error handling request: {e}", flush=True)
            finally:
                os.remove(REQ_READY)

        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bridge] stopped")
