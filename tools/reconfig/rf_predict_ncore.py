#!/usr/bin/env python3
"""
Runtime Reconfiguration N-Core Predictor (per-core model + shared-L3 model)

Reads one interval's stats (written by ReconfigurationManager::dumpIntervalStats(), the
current nested {"cores": [...], "l3": {...}} schema -- see reconfiguration_manager.cc)
from STATS_FILE, runs them through the N-core-generalized model pair trained by
/home/gina/Desktop/snipersim_framework/pythonScripts/randomForestNCoreGPU.py (per-core
model applied once per core + one shared L3 model), and writes a predicted configuration
to CONFIG_FILE in the same nested schema ReconfigurationManager::readConfigJSON() already
expects -- no C++-side changes needed to use this in place of rf_predict.py.

Unlike rf_predict.py (fixed to exactly 2 cores, core0/core1-suffixed), this scales to
however many cores are present in the stats file, matching the N-core generalization
already done on both the C++ side and the training side (see
/home/gina/.claude/plans/fluffy-tickling-shamir.md for the training design).

Known limitations (mirrors rf_predict.py's, plus one new one):
  - Same "simulator only supplies a small subset of the real training columns" gap as
    rf_predict.py -- confirmed by direct inspection of train_with_top3_barnes.csv's header:
    there is no ipc/miss-rate/branch-mpki column in the real training schema at all (those
    are simulator-only derived stats), so IPC_ALIASES below only covers what genuinely has a
    real-column match (L2/BTB/prefetch *previous config*, not performance counters). Anything
    else is 0-filled by the scaler-alignment step, same accepted/documented tradeoff.
  - percore_imputer.pkl / l3_imputer.pkl are optional here: randomForestNCoreGPU.py did not
    persist its SimpleImputer instances until this same change added it, so a model trained
    before that fix has no imputer file. This script falls back to skipping the imputer step
    (leaving 0-filled/NaN-scaled values as-is) rather than failing outright.
  - Preprocessing order at inference deliberately matches the training script's own
    test-time evaluation code exactly: scaler.transform() first, THEN imputer.transform()
    (see randomForestNCoreGPU.py's test_melted_scaled/test_l3_scaled construction) -- this
    looks backwards from the usual "impute before scaling" convention, but reordering it
    here would make this script diverge from what the training script's own self-check
    actually validated.

On any error (missing model, malformed stats, etc.) this script falls back to writing back
the current configuration unchanged and exits non-zero; the C++ side treats a non-zero exit
as "skip reconfiguration this interval."
"""

import glob
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import joblib

STATS_FILE = "/tmp/sniper_interval_stats.json"
CONFIG_FILE = "/tmp/sniper_new_config.json"
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")

KB_TO_BYTES = 1024  # l2/l3 targets are trained in KB, matching rf_predict.py's convention

# Real-column aliases for the per-core "own previous config" metrics -- cross-checked
# directly against train_with_top3_barnes.csv's header, not assumed. Column names come out
# of randomForestNCoreGPU.py's core_agnostic_key() applied to "L2 core 0_prev" / "BTB core
# 0_prev" / "Prefetch core 0_prev" (space-separated core token stripped, left as-is
# otherwise -- deliberately not "cleaned up" further so this matches the real trained
# feature name bit-for-bit). ipc/l1_miss_rate/l2_miss_rate/l3_miss_rate/branch_mpki have no
# match in the real schema at all (see module docstring) and are intentionally omitted here.
OWN_METRIC_ALIASES = {
    'l2_prev': 'L2 _prev',
    'btb_prev': 'BTB _prev',
    'prefetcher_prev': 'Prefetch _prev',
}
# metric keys that are genuinely numeric (for sibling aggregation / sum-if-count-like);
# prefetcher_prev is categorical and only ever produces NaN aggregates, same as training's
# own pd.to_numeric(..., errors='coerce') handling -- included for column-shape parity, not
# because the aggregate value itself is meaningful.
COUNT_LIKE_METRICS = {'l2_prev', 'btb_prev'}

SHARED_L3_PREV_COLUMN = 'L3_prev'  # no core token in the real schema -- chip-wide context


def load_stats(stats_file):
    try:
        with open(stats_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print("Error reading stats file %s: %s" % (stats_file, e), file=sys.stderr)
        return None


def find_latest_bundle(model_dir):
    """Locate the most recent ncore_<timestamp>_percore_model.pkl bundle and return its
    prefix (e.g. ".../model/ncore_20260906_120000")."""
    pkls = glob.glob(os.path.join(model_dir, "ncore_*_percore_model.pkl"))
    if not pkls:
        raise FileNotFoundError("No ncore_*_percore_model.pkl bundle found in %s" % model_dir)
    pkls.sort()
    return pkls[-1][:-len("_percore_model.pkl")]


def load_bundle(prefix):
    def _try_load(path):
        return joblib.load(path) if os.path.exists(path) else None

    percore_model = joblib.load(prefix + "_percore_model.pkl")
    percore_scaler = joblib.load(prefix + "_percore_scaler.pkl")
    percore_imputer = _try_load(prefix + "_percore_imputer.pkl")
    le_prefetch = joblib.load(prefix + "_percore_prefetch_encoder.pkl")

    l3_model = joblib.load(prefix + "_l3_model.pkl")
    l3_scaler = joblib.load(prefix + "_l3_scaler.pkl")
    l3_imputer = _try_load(prefix + "_l3_imputer.pkl")

    return percore_model, percore_scaler, percore_imputer, le_prefetch, l3_model, l3_scaler, l3_imputer


def _own_metrics_for_core(core_entry):
    """{real_column_name: value} for whatever this core's prev-config metrics alias to a
    real training column -- see OWN_METRIC_ALIASES."""
    out = {}
    for our_key, real_key in OWN_METRIC_ALIASES.items():
        if our_key in core_entry:
            out[real_key] = core_entry[our_key]
    return out


def _sibling_aggregate(cores, exclude_core_id):
    """Permutation-invariant median/p25/p75/iqr/max/min[/sum] summary of every OTHER
    core's aliased metrics, mirroring randomForestNCoreGPU.py's aggregate_core_features().
    Non-numeric values (prefetcher_prev) coerce to NaN, same as training's
    pd.to_numeric(..., errors='coerce')."""
    siblings = [c for c in cores if c.get('core_id') != exclude_core_id]
    out = {}
    for our_key, real_key in OWN_METRIC_ALIASES.items():
        vals = []
        for c in siblings:
            if our_key not in c:
                continue
            try:
                vals.append(float(c[our_key]))
            except (TypeError, ValueError):
                vals.append(np.nan)
        if not vals:
            continue
        arr = np.array(vals, dtype=float)
        if np.all(np.isnan(arr)):
            p25 = p75 = med = mx = mn = np.nan
        else:
            p25, p75 = np.nanpercentile(arr, 25), np.nanpercentile(arr, 75)
            med, mx, mn = np.nanmedian(arr), np.nanmax(arr), np.nanmin(arr)
        out['%s__median' % real_key] = med
        out['%s__p25' % real_key] = p25
        out['%s__p75' % real_key] = p75
        out['%s__iqr' % real_key] = p75 - p25
        out['%s__max' % real_key] = mx
        out['%s__min' % real_key] = mn
        if our_key in COUNT_LIKE_METRICS:
            out['%s__sum' % real_key] = np.nansum(arr) if not np.all(np.isnan(arr)) else np.nan
    return out


def build_percore_row(stats, core_entry):
    """One core's feature row: own aliased metrics (core-agnostic names) + sibling
    aggregate + own-vs-sibling deltas + shared L3_prev context. Matches
    melt_to_percore_rows()'s column shape; anything the real scaler doesn't expect is
    dropped, anything it expects that we don't have is 0-filled, by align_to_scaler()."""
    cores = stats.get('cores', [])
    core_id = core_entry.get('core_id')
    row = {}
    row.update(_own_metrics_for_core(core_entry))

    sibling_agg = _sibling_aggregate(cores, core_id)
    row.update(sibling_agg)

    for our_key, real_key in OWN_METRIC_ALIASES.items():
        if real_key not in row:
            continue
        med_col, max_col = '%s__median' % real_key, '%s__max' % real_key
        try:
            own_val = float(row[real_key])
        except (TypeError, ValueError):
            continue
        if med_col in sibling_agg:
            row['%s__delta_median' % real_key] = own_val - sibling_agg[med_col]
        if max_col in sibling_agg:
            row['%s__delta_max' % real_key] = own_val - sibling_agg[max_col]

    l3_prev = stats.get('l3', {}).get('l3_prev')
    if l3_prev is not None:
        row[SHARED_L3_PREV_COLUMN] = l3_prev

    return row


def build_l3_row(stats):
    """Chip-wide feature row for the shared L3 model: aggregate over ALL cores (no
    exclusion) + shared L3_prev context. Matches build_l3_frame()'s column shape."""
    cores = stats.get('cores', [])
    row = {}
    row.update(_sibling_aggregate(cores, exclude_core_id=None))
    l3_prev = stats.get('l3', {}).get('l3_prev')
    if l3_prev is not None:
        row[SHARED_L3_PREV_COLUMN] = l3_prev
    return row


def align_to_scaler(row, scaler):
    """1-row DataFrame reindexed to scaler.feature_names_in_ (0-filled where missing),
    the same alignment trick rf_predict.py already uses for the same documented reason."""
    df = pd.DataFrame([row])
    if hasattr(scaler, 'feature_names_in_'):
        expected = list(scaler.feature_names_in_)
        missing = [c for c in expected if c not in df.columns]
        if missing:
            df = pd.concat([df, pd.DataFrame(0.0, index=df.index, columns=missing)], axis=1)
        df = df[expected]
    return df


def apply_scaler_then_imputer(df, scaler, imputer):
    X = scaler.transform(df.values)
    if imputer is not None:
        X = imputer.transform(X)
    else:
        X = np.nan_to_num(X, nan=0.0)
    return X


def default_config_from_stats(stats):
    """Fallback: keep the current configuration unchanged, for however many cores are
    actually present in this interval's stats."""
    cores = stats.get('cores', [])
    out_cores = []
    for c in cores:
        out_cores.append({
            'core_id': c.get('core_id', 0),
            'l2_bytes': int(c.get('l2_prev', 0)),
            'btb_entries': int(c.get('btb_prev', 0)),
            'prefetch': str(c.get('prefetcher_prev', 'none')),
        })
    return {'cores': out_cores, 'l3': {'l3_bytes': int(stats.get('l3', {}).get('l3_prev', 0))}}


def write_config(config, config_file):
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print("Error writing config file %s: %s" % (config_file, e), file=sys.stderr)
        return False


def main():
    stats = load_stats(STATS_FILE)
    if stats is None or 'cores' not in stats:
        print("Failed to load stats (or missing 'cores'); cannot even fall back. Aborting.",
              file=sys.stderr)
        return 1

    fallback_config = default_config_from_stats(stats)

    try:
        prefix = find_latest_bundle(MODEL_DIR)
        (percore_model, percore_scaler, percore_imputer, le_prefetch,
         l3_model, l3_scaler, l3_imputer) = load_bundle(prefix)
    except Exception as e:
        print("Could not load ncore model bundle (%s); falling back to current configuration" % e,
              file=sys.stderr)
        write_config(fallback_config, CONFIG_FILE)
        return 1

    try:
        out_cores = []
        for core_entry in stats['cores']:
            row = build_percore_row(stats, core_entry)
            X = apply_scaler_then_imputer(align_to_scaler(row, percore_scaler), percore_scaler, percore_imputer)
            pred = percore_model.predict(X)[0]  # [l2__best, btb__best, prefetch__best]
            l2_kb, btb_entries, pf_idx = pred[0], pred[1], int(pred[2])
            pf_idx = max(0, min(pf_idx, len(le_prefetch.classes_) - 1))
            out_cores.append({
                'core_id': core_entry.get('core_id', 0),
                'l2_bytes': int(round(float(l2_kb))) * KB_TO_BYTES,
                'btb_entries': int(round(float(btb_entries))),
                'prefetch': str(le_prefetch.classes_[pf_idx]),
            })

        l3_row = build_l3_row(stats)
        X_l3 = apply_scaler_then_imputer(align_to_scaler(l3_row, l3_scaler), l3_scaler, l3_imputer)
        l3_kb = l3_model.predict(X_l3)[0]

        config = {'cores': out_cores, 'l3': {'l3_bytes': int(round(float(l3_kb))) * KB_TO_BYTES}}
    except Exception as e:
        print("Error running N-core RF prediction (%s); falling back to current configuration" % e,
              file=sys.stderr)
        write_config(fallback_config, CONFIG_FILE)
        return 1

    if write_config(config, CONFIG_FILE):
        print("N-core configuration prediction successful: %s" % config, file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
