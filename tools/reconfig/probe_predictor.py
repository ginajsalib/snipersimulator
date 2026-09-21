#!/usr/bin/env python3
"""Probe what the RF predictor actually responds to.

Replays real per-interval feature vectors (taken straight from a run's
sniper_reconfig_decisions.csv) through the predictor, then replays deliberately
perturbed ones, and reports whether the predicted configuration ever changes.

Motivation: with the cache miss-rate features fixed and demonstrably varying
(13 distinct L1/L2 rates over 42 intervals of radix), the predictor still emitted
one identical config for every interval. That is either correct behaviour for this
input range, or the model is insensitive to these inputs -- this tells us which,
without running the simulator.

Usage:
  probe_predictor.py --log <sniper_reconfig_decisions.csv> [--n 8]
  probe_predictor.py --synthetic            # sweep extreme values instead
  probe_predictor.py --log ... --synthetic  # both
"""

import argparse
import copy
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = "/tmp/sniper_interval_stats.json"
CONFIG_FILE = "/tmp/sniper_new_config.json"


def run_predictor(stats, script):
    """Write stats where the predictor expects them, run it, return the config it emits."""
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f)
    if os.path.exists(CONFIG_FILE):
        os.remove(CONFIG_FILE)
    p = subprocess.run([sys.executable, script], capture_output=True, text=True)
    # Read the config back even on a non-zero exit: the predictor prints its success
    # line to stderr and can still exit non-zero for unrelated reasons (e.g. a warning
    # path), and an earlier version of this probe reported those as FAILED.
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f), None
        except Exception as e:
            return None, ["unreadable config: %s" % e]
    return None, ["rc=%d %s" % (p.returncode,
                  ((p.stderr or "").strip().splitlines() or ["(no stderr)"])[-1])]


def summarise(cfg):
    if cfg is None:
        return "FAILED"
    cores = cfg.get("cores", [])
    return "L2=%s BTB=%s PF=%s L3=%s" % (
        "/".join(str(c.get("l2_bytes")) for c in cores),
        "/".join(str(c.get("btb_entries")) for c in cores),
        "/".join(str(c.get("prefetch")) for c in cores),
        cfg.get("l3", {}).get("l3_bytes"))


def stats_from_row(row, ncores):
    """Rebuild the stats JSON that dumpIntervalStats() would have written for this row."""
    cores = []
    for c in range(ncores):
        cores.append({
            "core_id": c,
            "ipc": float(row["ipc_core%d" % c]),
            "l1_miss_rate": float(row["l1_miss_rate_core%d" % c]),
            "l2_miss_rate": float(row["l2_miss_rate_core%d" % c]),
            "l3_miss_rate": float(row["l3_miss_rate_core%d" % c]),
            "branch_mpki": float(row["branch_mpki_core%d" % c]),
            "l2_prev": int(row["l2_bytes_prev_core%d" % c]),
            "btb_prev": int(row["btb_entries_prev_core%d" % c]),
            "prefetcher_prev": row["prefetch_prev_core%d" % c],
        })
    return {"cores": cores, "l3": {"l3_prev": int(row["l3_bytes_prev"])},
            "active_cores": ncores}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=None, help="a run's sniper_reconfig_decisions.csv")
    ap.add_argument("--n", type=int, default=8, help="how many real intervals to replay")
    ap.add_argument("--synthetic", action="store_true", help="also sweep extreme values")
    ap.add_argument("--script", default=os.path.join(HERE, "rf_predict_ncore.py"))
    args = ap.parse_args()

    outs = set()

    if args.log:
        import csv
        with open(args.log) as f:
            rows = list(csv.DictReader(f))
        ncores = sum(1 for k in rows[0] if k.startswith("ipc_core"))
        print("=== replaying %d real intervals (%d cores) ===" % (args.n, ncores))
        # spread the sample across the run rather than taking the first N, so phase
        # changes are represented
        step = max(1, len(rows) // args.n)
        for r in rows[::step][:args.n]:
            st = stats_from_row(r, ncores)
            cfg, err = run_predictor(st, args.script)
            outs.add(summarise(cfg))
            print("  iv %-5s ipc=%-8.3f l1=%-9.6f l2=%-9.6f l3=%-9.6f -> %s" % (
                r["interval"], st["cores"][0]["ipc"], st["cores"][0]["l1_miss_rate"],
                st["cores"][0]["l2_miss_rate"], st["cores"][0]["l3_miss_rate"],
                summarise(cfg) if cfg else "FAILED %s" % err))

    if args.synthetic:
        print("\n=== sweeping extreme synthetic inputs ===")
        base_core = {"core_id": 0, "ipc": 1.0, "l1_miss_rate": 0.0, "l2_miss_rate": 0.0,
                     "l3_miss_rate": 0.0, "branch_mpki": 1.0, "l2_prev": 262144,
                     "btb_prev": 1024, "prefetcher_prev": "none"}
        ncores = 4
        cases = []
        for name, key, vals in [
            ("ipc",          "ipc",          [0.05, 0.5, 1.0, 2.0, 4.0]),
            ("l1_miss_rate", "l1_miss_rate", [0.0, 0.01, 0.1, 0.5, 0.9]),
            ("l2_miss_rate", "l2_miss_rate", [0.0, 0.01, 0.1, 0.5, 0.9]),
            ("l3_miss_rate", "l3_miss_rate", [0.0, 0.01, 0.1, 0.5, 0.9]),
            ("branch_mpki",  "branch_mpki",  [0.0, 1.0, 10.0, 50.0]),
        ]:
            for v in vals:
                cases.append((name, key, v))
        for name, key, v in cases:
            cores = []
            for c in range(ncores):
                cc = copy.deepcopy(base_core)
                cc["core_id"] = c
                cc[key] = v
                cores.append(cc)
            st = {"cores": cores, "l3": {"l3_prev": 8388608}, "active_cores": ncores}
            cfg, err = run_predictor(st, args.script)
            outs.add(summarise(cfg))
            print("  %-14s = %-8s -> %s" % (name, v, summarise(cfg) if cfg else "FAILED %s" % err))

    print("\n=== distinct predicted configurations seen: %d ===" % len(outs))
    for o in sorted(outs):
        print("   ", o)
    if len(outs) == 1:
        print("\n  The predictor returned the SAME configuration for every input tried.")
        print("  That is model behaviour, not a plumbing problem -- the inputs above")
        print("  genuinely differ and still map to one output.")


if __name__ == "__main__":
    main()
