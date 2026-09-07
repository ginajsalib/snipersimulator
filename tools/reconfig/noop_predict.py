#!/usr/bin/env python3
"""
No-op "predictor" -- echoes the current (prev) configuration back unchanged, every
interval. Used as the `no_change` and `best_static` arms of the final experiment
(see tools/reconfig/FINAL_EXPERIMENT.md): both still run with reconfig/enabled=true
so they get the exact same per-interval McPAT sampling as the dynamic_rf arm (same
instrumentation across all arms is the point -- only whether the config actually
moves differs), they just never change what ReconfigurationManager applies.

  no_change:   base.cfg's own starting config, left alone for the whole run.
  best_static: base.cfg's starting config overridden (via -g) to the chosen
               "best static" values for that benchmark; this script then holds
               it there for the whole run, identically to no_change.

Reads/writes the same nested JSON schema as rf_predict.py/rf_predict_ncore.py
(ReconfigurationManager::dumpIntervalStats()/readConfigJSON()) -- core-count
agnostic, no aliasing needed, since "echo the current config back" doesn't
depend on any trained model's feature schema.

Always exits 0: this is normal, intended behavior, not a fallback path.
"""

import json
import sys

STATS_FILE = "/tmp/sniper_interval_stats.json"
CONFIG_FILE = "/tmp/sniper_new_config.json"


def main():
    try:
        with open(STATS_FILE, 'r') as f:
            stats = json.load(f)
    except Exception as e:
        print("Error reading stats file %s: %s" % (STATS_FILE, e), file=sys.stderr)
        return 1

    cores = stats.get('cores', [])
    out_cores = []
    for c in cores:
        out_cores.append({
            'core_id': c.get('core_id', 0),
            'l2_bytes': int(c.get('l2_prev', 0)),
            'btb_entries': int(c.get('btb_prev', 0)),
            'prefetch': str(c.get('prefetcher_prev', 'none')),
        })
    config = {'cores': out_cores, 'l3': {'l3_bytes': int(stats.get('l3', {}).get('l3_prev', 0))}}

    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print("Error writing config file %s: %s" % (CONFIG_FILE, e), file=sys.stderr)
        return 1

    print("noop_predict: echoed current config unchanged: %s" % config, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
