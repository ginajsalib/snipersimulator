"""Selectable objective metric for the best-config / top-3 selection stages.

The pipeline originally hardcoded PPW = ips^3 / power. That is the reciprocal of the
energy-delay-squared product, so it weights performance very heavily -- a 5x throughput
change moves it by 125x. These alternatives let the same selection machinery be driven
by other objectives without touching the ranking logic.

  ppw      ips^3 / power   higher is better   (original default, = 1/ED^2P)
  ppw_new  ips   / power   higher is better   (conventional performance-per-watt)
  ips      ips             higher is better   (throughput only, power ignored)
  power    power           LOWER  is better   (power only, performance ignored)

add_objective() always produces a column where HIGHER IS BETTER -- for `power` it
negates -- so callers can keep using idxmax()/sort(ascending=False) unchanged. The raw
(un-negated) value is returned separately for reporting, so a `power` run reports watts
rather than negative watts.
"""

import pandas as pd

OBJECTIVE_COL = '_objective'
METRICS = ('ppw', 'ppw_new', 'ips', 'power')
HIGHER_IS_BETTER = {'ppw': True, 'ppw_new': True, 'ips': True, 'power': False}


def _find(df, *candidates):
    norm = lambda s: s.lower().replace('.', '').replace('_', '').replace(' ', '')
    for cand in candidates:
        for col in df.columns:
            if norm(col) == norm(cand):
                return col
    for cand in candidates:
        for col in df.columns:
            if norm(cand) in norm(col):
                return col
    return None


def add_objective(df, metric='ppw'):
    """Add OBJECTIVE_COL (higher==better) and return (objective_col, raw_col, higher_better).

    raw_col holds the metric's natural value for reporting; for every metric except
    `power` it is identical to the objective.
    """
    if metric not in METRICS:
        raise ValueError('unknown metric %r (choose from %s)' % (metric, ', '.join(METRICS)))

    ips_col   = _find(df, 'ips')
    power_col = _find(df, 'total_power', 'total_runtime_dynamic')
    ppw_col   = _find(df, 'ppw')

    if metric == 'ppw':
        if ppw_col is None:
            raise ValueError('no ppw column; available: %s' % list(df.columns)[:20])
        raw = ppw_col
    elif metric == 'ips':
        if ips_col is None:
            raise ValueError('no ips column')
        raw = ips_col
    elif metric == 'power':
        if power_col is None:
            raise ValueError('no power column (total_power / total_runtime_dynamic)')
        raw = power_col
    else:  # ppw_new
        if ips_col is None or power_col is None:
            raise ValueError('ppw_new needs both ips and power columns')
        raw = 'ppw_new'
        df[raw] = pd.to_numeric(df[ips_col], errors='coerce') / \
                  pd.to_numeric(df[power_col], errors='coerce')

    df[raw] = pd.to_numeric(df[raw], errors='coerce')
    # Negate rather than invert for `power`: 1/x would distort the magnitude of the
    # best-vs-second differences the top-3 stage reports.
    df[OBJECTIVE_COL] = df[raw] if HIGHER_IS_BETTER[metric] else -df[raw]
    return OBJECTIVE_COL, raw, HIGHER_IS_BETTER[metric]


def metric_from_argv(argv, default='ppw'):
    """Pull `--metric X` out of argv in place, returning (metric, remaining_argv)."""
    out, metric = [], default
    i = 0
    while i < len(argv):
        if argv[i] == '--metric' and i + 1 < len(argv):
            metric = argv[i + 1]; i += 2; continue
        if argv[i].startswith('--metric='):
            metric = argv[i].split('=', 1)[1]; i += 1; continue
        out.append(argv[i]); i += 1
    return metric, out
