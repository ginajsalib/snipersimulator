#!/usr/bin/env python3
"""
Runtime Reconfiguration RF Model Predictor (7-way)

Reads one interval's stats (written by ReconfigurationManager::dumpIntervalStats() in
common/reconfig/reconfiguration_manager.cc) from STATS_FILE, runs them through the real
trained 7-way RF model in tools/reconfig/model/, and writes a predicted configuration to
CONFIG_FILE for the simulator to apply.

The model jointly predicts 7 targets, in this order: l2_core0, l2_core1, l3, btb_core0,
btb_core1, prefetch_core0, prefetch_core1 (see TARGET_KEYS in
/home/gina/Desktop/snipersim_framework/pythonScripts/predict_config.py, which this script's
preprocessing mirrors: align the input row to the scaler's feature_names_in_, 0-filling
anything the simulator doesn't provide).

Known limitations (see the RF reconfiguration integration plan for detail):
  - The simulator only supplies a small, cleanly-computable subset of the ~90 raw stat
    columns the model was actually trained on (IPC, miss rates, branch MPKI, prev config).
    Everything else is 0-filled by the scaler-alignment step below -- degraded but
    functional, not broken.
  - l2_core0/l2_core1/l3 are assumed to be predicted in KB (matching Sniper's own
    perf_model/*/cache_size config convention) and are converted to bytes here for the
    C++ side, which operates in bytes. This is an assumption, not a verified fact about
    the training data -- if reconfigured cache sizes come out implausible, check this
    conversion first.
  - The model's own metadata reports ~0.16% exact 7-way match accuracy and 25-50%
    per-dimension accuracy. This is a research/experimentation feature, not a tuned
    production optimizer.

On any error (missing model, malformed stats, etc.) this script falls back to writing
back the current configuration unchanged and exits non-zero; the C++ side treats a
non-zero exit as "skip reconfiguration this interval."
"""

import glob
import json
import os
import sys

import numpy as np
import pandas as pd
import joblib

STATS_FILE = "/tmp/sniper_interval_stats.json"
CONFIG_FILE = "/tmp/sniper_new_config.json"
MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model")

TARGET_KEYS = ['l2_core0', 'l2_core1', 'l3', 'btb_core0', 'btb_core1',
               'prefetch_core0', 'prefetch_core1']

KB_TO_BYTES = 1024  # see the "Known limitations" note above

# Best-effort aliases from the simplified stat names the simulator writes to plausible
# real training-column spellings (cross-checked against the training CSV header at
# /home/gina/Desktop/snipersim_framework/pythonScripts/finalTrainingData/*.csv). Anything
# not listed here, or that doesn't end up matching the scaler's feature_names_in_ exactly,
# is simply 0-filled below rather than causing an error.
ALIASES = {
    'l3_prev': 'L3_prev',
    'btbcore0_prev': 'btbCore0_prev',
    'btbcore1_prev': 'btbCore1_prev',
    'prefetcher_core0_prev': 'prefetcher_core0_prev',
    'prefetcher_core1_prev': 'prefetcher_core1_prev',
}


def load_stats(stats_file):
    """Load interval statistics from JSON file."""
    try:
        with open(stats_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading stats file {stats_file}: {e}", file=sys.stderr)
        return None


def flatten_stats(stats):
    """Adapt dumpIntervalStats()'s current N-core-generalized schema
    ({"cores": [{"core_id":0, "ipc":..., "l2_prev":..., ...}, ...], "l3": {"l3_prev":...},
    "active_cores": N}) back to the flat, core0/core1-suffixed keys this script (and the
    underlying 7-way model, which is trained on exactly 2 cores -- see the "Assumption"
    note in RECONFIGURATION_CHANGES.md) expects. A stats dict without a "cores" key (e.g.
    a hand-written test fixture using the old flat schema directly) is passed through
    unchanged."""
    if 'cores' not in stats:
        return stats

    flat = {}
    for core in stats['cores']:
        c = core.get('core_id')
        if c is None:
            continue
        flat['ipc_core%d' % c] = core.get('ipc', 0)
        flat['l1_miss_rate_core%d' % c] = core.get('l1_miss_rate', 0)
        flat['l2_miss_rate_core%d' % c] = core.get('l2_miss_rate', 0)
        flat['l3_miss_rate_core%d' % c] = core.get('l3_miss_rate', 0)
        flat['branch_mpki_core%d' % c] = core.get('branch_mpki', 0)
        flat['l2core%d_prev' % c] = core.get('l2_prev', 0)
        flat['btbcore%d_prev' % c] = core.get('btb_prev', 0)
        flat['prefetcher_core%d_prev' % c] = core.get('prefetcher_prev', 'none')
    flat['l3_prev'] = stats.get('l3', {}).get('l3_prev', 0)
    flat['active_cores'] = stats.get('active_cores', len(stats['cores']))
    return flat


def load_model_bundle(model_dir):
    """Load the most recent saved 7-way RF model and its scaler/prefetcher encoders."""
    pkls = glob.glob(os.path.join(model_dir, "rf_7way_config_predictor_*.pkl"))
    pkls = [p for p in pkls if not any(tag in p for tag in ('_scaler', '_encoder', '_imputer'))]
    if not pkls:
        raise FileNotFoundError(f"No 7-way RF model .pkl found in {model_dir}")
    pkls.sort()
    model_path = pkls[-1]
    base = model_path[:-len('.pkl')]

    model = joblib.load(model_path)
    scaler = joblib.load(base + '_scaler.pkl')
    enc_pf0 = joblib.load(base + '_prefetcher_core0_encoder.pkl')
    enc_pf1 = joblib.load(base + '_prefetcher_core1_encoder.pkl')
    return model, scaler, enc_pf0, enc_pf1


def build_feature_row(stats, scaler):
    """Turn one interval's flat stats dict into a 1-row feature matrix aligned to the
    scaler's expected columns (0-filled where the simulator didn't provide a value)."""
    row = dict(stats)
    for our_name, real_name in ALIASES.items():
        if our_name in row and real_name not in row:
            row[real_name] = row.pop(our_name)

    df = pd.DataFrame([row])

    # Encode any leftover string-valued columns (e.g. prefetcher_core0_prev) the same
    # crude way predict_config.py does for inference-time (non-target) features.
    categorical_cols = df.select_dtypes(include=['object']).columns
    for col in categorical_cols:
        df[col] = df[col].astype('category').cat.codes

    if scaler is not None and hasattr(scaler, 'feature_names_in_'):
        expected = list(scaler.feature_names_in_)
        missing = [c for c in expected if c not in df.columns]
        if missing:
            df = pd.concat([df, pd.DataFrame(0.0, index=df.index, columns=missing)], axis=1)
        df = df[expected]

    return df


def default_config_from_stats(stats):
    """Fallback: keep the current configuration unchanged."""
    l2_prev = int(stats.get('l2core0_prev', 0))
    return {
        'l2_core0': l2_prev,
        'l2_core1': int(stats.get('l2core1_prev', l2_prev)),
        'l3': int(stats.get('l3_prev', 0)),
        'btb_core0': int(stats.get('btbcore0_prev', 0)),
        'btb_core1': int(stats.get('btbcore1_prev', 0)),
        'prefetch_core0': str(stats.get('prefetcher_core0_prev', 'none')),
        'prefetch_core1': str(stats.get('prefetcher_core1_prev', 'none')),
    }


def write_config(config, config_file):
    """Write predicted configuration to JSON file."""
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error writing config file {config_file}: {e}", file=sys.stderr)
        return False


def main():
    stats = load_stats(STATS_FILE)
    if stats is None:
        print("Failed to load stats; cannot even fall back to current config. Aborting.", file=sys.stderr)
        return 1
    stats = flatten_stats(stats)

    fallback_config = default_config_from_stats(stats)

    try:
        model, scaler, enc_pf0, enc_pf1 = load_model_bundle(MODEL_DIR)
    except Exception as e:
        print(f"Could not load RF model ({e}); falling back to current configuration", file=sys.stderr)
        write_config(fallback_config, CONFIG_FILE)
        return 1

    try:
        X = build_feature_row(stats, scaler)
        X_scaled = scaler.transform(X) if scaler is not None else X.values

        y_pred = model.predict(X_scaled)
        idx = {key: i for i, key in enumerate(TARGET_KEYS)}

        pf0_idx = int(y_pred[0][idx['prefetch_core0']])
        pf1_idx = int(y_pred[0][idx['prefetch_core1']])
        pf0 = enc_pf0.classes_[np.clip(pf0_idx, 0, len(enc_pf0.classes_) - 1)]
        pf1 = enc_pf1.classes_[np.clip(pf1_idx, 0, len(enc_pf1.classes_) - 1)]

        config = {
            'l2_core0': int(y_pred[0][idx['l2_core0']]) * KB_TO_BYTES,
            'l2_core1': int(y_pred[0][idx['l2_core1']]) * KB_TO_BYTES,
            'l3': int(y_pred[0][idx['l3']]) * KB_TO_BYTES,
            'btb_core0': int(y_pred[0][idx['btb_core0']]),
            'btb_core1': int(y_pred[0][idx['btb_core1']]),
            'prefetch_core0': str(pf0),
            'prefetch_core1': str(pf1),
        }
    except Exception as e:
        print(f"Error running RF prediction ({e}); falling back to current configuration", file=sys.stderr)
        write_config(fallback_config, CONFIG_FILE)
        return 1

    if write_config(config, CONFIG_FILE):
        print(f"Configuration prediction successful: {config}", file=sys.stderr)
        return 0
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
