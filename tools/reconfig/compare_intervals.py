#!/usr/bin/env python2
"""
Compare two reconfiguration-interval settings cell by cell.

Takes two CSVs written by compare_shrink_policies.py --csv (one per interval length) and
reports, for every (benchmark, topology, arm) present in both, how each metric moved.

  python2 tools/reconfig/compare_intervals.py \
    --baseline ~/v2_final_per_arm.csv --baseline-label 1M \
    --other    ~/v3_500k_per_arm.csv  --other-label 500k \
    --csv ~/interval_comparison.csv

Ratios are other / baseline, so >1 means the shorter interval produced more of that
metric. For hit rates the difference in percentage points is reported instead, since a
ratio of two percentages is not meaningful.

Read the result with two confounds in mind.

  1. The RF model was trained on 1,000,000-instruction intervals. Halving the interval
     halves every delta feature it is handed, so a 500k run measures finer decision
     granularity AND out-of-distribution model inputs at the same time. A difference here
     does not by itself say granularity helped or hurt.
  2. Halving the interval doubles the number of McPAT samples, which makes the power
     trace finer but does not change what it measures. intervals_x is reported so you can
     confirm the interval actually changed (it should be ~2.0).
"""

import argparse
import collections
import csv
import math
import os
import sys

# (column in the per-arm CSV, short name, how to combine across cells)
RATIO_METRICS = [
    ('IPC', 'ipc', 'geo'),
    ('IPS', 'ips', 'geo'),
    ('W dyn', 'w_dyn', 'geo'),
    ('W tot', 'w_total', 'geo'),
    ('E dyn (J)', 'e_dyn', 'geo'),
    ('E tot (J)', 'e_total', 'geo'),
    ('IPS/W dyn', 'ips_per_w_dyn', 'geo'),
    ('IPS/W tot', 'ips_per_w_total', 'geo'),
    ('PPW dyn', 'ppw_dyn', 'geo'),
    ('PPW tot', 'ppw_total', 'geo'),
]
DELTA_METRICS = [
    ('L1-D hit', 'l1d_hit_pp'),
    ('L2 hit', 'l2_hit_pp'),
    ('L3 hit', 'l3_hit_pp'),
    ('L2 got', 'l2_shrink_pp'),
    ('L3 got', 'l3_shrink_pp'),
]
TOPO_ORDER = ['4c 2P+2E', '8c 2P+6E', '8c 4P+4E']


def num(x):
    try:
        return float(str(x).replace('%', '').strip())
    except (TypeError, ValueError):
        return None


def load(path):
    out = {}
    with open(path) as fh:
        for r in csv.DictReader(fh):
            out[(r['benchmark'], r['topology'], r['arm'])] = r
    if not out:
        sys.exit('ERROR: %s has no rows' % path)
    return out


def geo(xs):
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--baseline', required=True, help='per-arm CSV for the reference interval')
    p.add_argument('--other', required=True, help='per-arm CSV for the interval being tested')
    p.add_argument('--baseline-label', default='base')
    p.add_argument('--other-label', default='other')
    p.add_argument('--csv', default=None, help='write the per-cell table here')
    args = p.parse_args()

    base, other = load(args.baseline), load(args.other)
    shared = sorted(set(base) & set(other),
                    key=lambda k: (k[2], k[0], TOPO_ORDER.index(k[1])
                                   if k[1] in TOPO_ORDER else 99))
    if not shared:
        sys.exit('ERROR: the two CSVs share no (benchmark, topology, arm) rows')
    only_b, only_o = set(base) - set(other), set(other) - set(base)

    cols = ['benchmark', 'topology', 'arm', 'intervals_x'] + \
           [s for _, s, _ in RATIO_METRICS] + [s for _, s in DELTA_METRICS]
    recs = []
    acc = collections.defaultdict(lambda: collections.defaultdict(list))

    for key in shared:
        b, o = base[key], other[key]
        rec = {'benchmark': key[0], 'topology': key[1], 'arm': key[2]}
        bi, oi = num(b.get('intervals')), num(o.get('intervals'))
        rec['intervals_x'] = round(oi / bi, 3) if (bi and oi) else ''
        for col, short, _ in RATIO_METRICS:
            x, y = num(o.get(col)), num(b.get(col))
            if x is not None and y:
                rec[short] = round(x / y, 4)
                acc[key[2]][short].append(x / y)
            else:
                rec[short] = ''
        for col, short in DELTA_METRICS:
            x, y = num(o.get(col)), num(b.get(col))
            if x is not None and y is not None:
                rec[short] = round(x - y, 2)
                acc[key[2]][short].append(x - y)
            else:
                rec[short] = ''
        recs.append(rec)

    for arm in sorted(acc):
        g = dict.fromkeys(cols, '')
        g['benchmark'] = 'GEOMEAN' if True else ''
        g['topology'] = '%d cells' % len(acc[arm].get('ips', []))
        g['arm'] = arm
        for _, short, _ in RATIO_METRICS:
            v = geo(acc[arm][short])
            g[short] = round(v, 4) if v is not None else ''
        for _, short in DELTA_METRICS:   # mean of percentage-point deltas, not geomean
            v = mean(acc[arm][short])
            g[short] = round(v, 2) if v is not None else ''
        recs.append(g)

    show = ['arm', 'benchmark', 'topology', 'intervals_x', 'ips', 'w_total',
            'e_total', 'ips_per_w_total', 'ppw_total', 'l2_hit_pp', 'l3_hit_pp']
    widths = [max(len(str(r.get(c, ''))) for r in recs + [dict((c, c) for c in show)])
              for c in show]
    line = lambda r: '  '.join(str(r.get(c, '')).ljust(widths[i]) for i, c in enumerate(show))
    print('%s  ->  %s   (ratios are %s / %s)' % (
        args.baseline_label, args.other_label, args.other_label, args.baseline_label))
    print('')
    print(line(dict((c, c) for c in show)))
    print('-' * (sum(widths) + 2 * (len(show) - 1)))
    last = None
    for r in recs:
        if last is not None and r['arm'] != last:
            print('')
        print(line(r))
        last = r['arm']

    if only_b or only_o:
        print('')
        print('cells only in %s: %d   only in %s: %d' % (
            args.baseline_label, len(only_b), args.other_label, len(only_o)))

    if args.csv:
        with open(args.csv, 'wb') as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(recs)
        print('')
        print('full table -> %s' % args.csv)


if __name__ == '__main__':
    main()
