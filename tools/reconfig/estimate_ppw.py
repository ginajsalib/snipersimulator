#!/usr/bin/env python3
"""
Estimate PPW impact of reconfiguration decisions, without running Sniper/McPAT.

Loads the PPW surrogate regressor trained (as a side effect of Validation step 3) by
randomForestNCoreGPU.py -- a RandomForestRegressor that predicts PPW directly from a
candidate config + system context, fit on real simulated PPW ground truth -- and uses
it to score every "applied" row of the reconfig CSV decision log
(common/reconfig/reconfiguration_manager.cc's logDecision(), default
/tmp/sniper_reconfig_decisions.csv) twice: once for the config that was live going into
that interval ("prev"), once for what the RF model actually decided to switch to
("new"). The difference is an approximation of whether that decision helped, with no
new simulation required.

IMPORTANT CAVEAT (surfaced explicitly, not just in this docstring): the surrogate is
trained and evaluated on 2-core data only (see its own metadata.txt's "Surrogate
trustworthy" line, gated on Pearson r >= 0.8). Using it to score N>2-core decisions is
an unvalidated extrapolation -- this script warns loudly when doing so, it does not
pretend otherwise. Real confidence for N>2 cores requires the small targeted 3-core
simulation set described in /home/gina/.claude/plans/fluffy-tickling-shamir.md, which
has not been collected yet.

Usage:
  python3 estimate_ppw.py [--decision-log PATH] [--model-dir DIR] [--benchmark NAME]
"""

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd
import joblib

DEFAULT_DECISION_LOG = "/tmp/sniper_reconfig_decisions.csv"
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")

# The one chip-wide "_prev" context column our simulator can actually supply, aliased
# to the real training-schema name -- same mapping rf_predict_ncore.py uses, and for
# the same reason (cross-checked against train_with_top3_barnes.csv's header directly,
# not assumed). Everything else the surrogate's shared-context features expect is
# 0-filled by the scaler/imputer alignment below, same documented tradeoff as elsewhere.
SHARED_L3_PREV_COLUMN = "L3_prev"


def find_latest_bundle(model_dir):
    pkls = glob.glob(os.path.join(model_dir, "ncore_*_surrogate_model.pkl"))
    if not pkls:
        raise FileNotFoundError(
            "No ncore_*_surrogate_model.pkl found in %s -- the surrogate is only "
            "produced (and persisted) by a completed randomForestNCoreGPU.py run." % model_dir)
    pkls.sort()
    return pkls[-1][:-len("_surrogate_model.pkl")]


def load_bundle(prefix):
    model = joblib.load(prefix + "_surrogate_model.pkl")
    imputer = joblib.load(prefix + "_surrogate_imputer.pkl")
    encoders = joblib.load(prefix + "_surrogate_encoders.pkl")
    return model, imputer, encoders


def detect_core_ids_from_decision_log(columns):
    """core ids present as l2_bytes_<new|prev>_core<N> columns in the decision log."""
    ids = set()
    for c in columns:
        if c.startswith("l2_bytes_prev_core"):
            try:
                ids.add(int(c[len("l2_bytes_prev_core"):]))
            except ValueError:
                pass
    return sorted(ids)


def encode_categorical(row, col, value, encoders):
    """Mirrors randomForestNCoreGPU.py's encode_and_numeric() transform-with-existing-
    encoder branch: unseen categories fall back to the encoder's first class rather
    than raising, with a warning so that fallback isn't silent."""
    le = encoders.get(col)
    if le is None:
        return
    value = str(value)
    if value not in set(le.classes_):
        print("Warning: %s=%r unseen by the surrogate's encoder; using %r instead" %
              (col, value, le.classes_[0]), file=sys.stderr)
        value = le.classes_[0]
    row[col] = int(le.transform([value])[0])


def build_row(core_ids, l2_bytes, btb_entries, prefetch, l3_bytes, benchmark, l3_prev_ctx, encoders):
    row = {}
    for n in core_ids:
        row["l2_c%d" % n] = l2_bytes[n] / 1024.0  # bytes -> KB, matching training's convention
        row["btb_c%d" % n] = btb_entries[n]
        encode_categorical(row, "pf_c%d" % n, prefetch[n], encoders)
    row["l3"] = l3_bytes / 1024.0
    if l3_prev_ctx is not None:
        row[SHARED_L3_PREV_COLUMN] = l3_prev_ctx / 1024.0
    encode_categorical(row, "benchmark", benchmark, encoders)
    return row


def align_and_predict(row, model, imputer):
    df = pd.DataFrame([row])
    if hasattr(imputer, "feature_names_in_"):
        expected = list(imputer.feature_names_in_)
        missing = [c for c in expected if c not in df.columns]
        if missing:
            df = pd.concat([df, pd.DataFrame(0.0, index=df.index, columns=missing)], axis=1)
        df = df[expected]
    X = imputer.transform(df.fillna(0.0).values)
    return float(model.predict(X)[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--decision-log", default=DEFAULT_DECISION_LOG)
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--benchmark", default=None,
                     help="Which benchmark was running (barnes/cholesky/fft/radiosity, or "
                          "whatever the surrogate was trained on) -- the surrogate needs this "
                          "as a feature; omitting it falls back to the encoder's first class.")
    ap.add_argument("--out", default=None, help="Optional CSV to write per-interval results to.")
    args = ap.parse_args()

    prefix = find_latest_bundle(args.model_dir)
    model, imputer, encoders = load_bundle(prefix)
    print("Using surrogate bundle: %s" % prefix, file=sys.stderr)

    log = pd.read_csv(args.decision_log)
    log = log[log["status"] == "applied"].reset_index(drop=True)
    if log.empty:
        print("No 'applied' rows in %s -- nothing to evaluate." % args.decision_log, file=sys.stderr)
        return 1

    core_ids = detect_core_ids_from_decision_log(log.columns)
    if not core_ids:
        print("Could not detect any core columns (l2_bytes_prev_core<N>) in %s" % args.decision_log,
              file=sys.stderr)
        return 1
    if len(core_ids) > 2:
        print("WARNING: %d cores detected -- the surrogate is trained/validated on 2-core data "
              "only. These estimates are an unvalidated extrapolation, not a proven-accurate "
              "prediction. Treat them as a rough signal, not ground truth." % len(core_ids),
              file=sys.stderr)

    results = []
    for _, r in log.iterrows():
        l2_prev = {n: r["l2_bytes_prev_core%d" % n] for n in core_ids}
        l2_new = {n: r["l2_bytes_new_core%d" % n] for n in core_ids}
        btb_prev = {n: r["btb_entries_prev_core%d" % n] for n in core_ids}
        btb_new = {n: r["btb_entries_new_core%d" % n] for n in core_ids}
        pf_prev = {n: r["prefetch_prev_core%d" % n] for n in core_ids}
        pf_new = {n: r["prefetch_new_core%d" % n] for n in core_ids}

        prev_row = build_row(core_ids, l2_prev, btb_prev, pf_prev,
                              r["l3_bytes_prev"], args.benchmark, r["l3_bytes_prev"], encoders)
        new_row = build_row(core_ids, l2_new, btb_new, pf_new,
                             r["l3_bytes_new"], args.benchmark, r["l3_bytes_prev"], encoders)

        ppw_prev = align_and_predict(prev_row, model, imputer)
        ppw_new = align_and_predict(new_row, model, imputer)
        pct_change = (ppw_new - ppw_prev) / ppw_prev * 100.0 if ppw_prev else float("nan")

        results.append({
            "interval": r["interval"],
            "estimated_ppw_prev": ppw_prev,
            "estimated_ppw_new": ppw_new,
            "pct_change": pct_change,
        })

    out_df = pd.DataFrame(results)
    print(out_df.to_string(index=False))
    print("\nSummary across %d applied intervals:" % len(out_df))
    print("  mean %% change:   %.2f%%" % out_df["pct_change"].mean())
    print("  median %% change: %.2f%%" % out_df["pct_change"].median())
    print("  improved: %d, regressed: %d, ~unchanged: %d" % (
        (out_df["pct_change"] > 1).sum(), (out_df["pct_change"] < -1).sum(),
        (out_df["pct_change"].abs() <= 1).sum()))

    if args.out:
        out_df.to_csv(args.out, index=False)
        print("\nWrote %s" % args.out, file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
