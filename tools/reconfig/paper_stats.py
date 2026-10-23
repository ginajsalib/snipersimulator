#!/usr/bin/env python2
"""
Summary statistics over the final flush campaign, for the paper. READ-ONLY: reads
results trees and writes two files under tools/reconfig/. Runs nothing.

  python2 tools/reconfig/paper_stats.py \
      --results-root /home/gina/Desktop/dockerMnt/sniperCodeNewBranch-centos6/results

Six items, each reported per topology and pooled over all runs:
  1. cache/BTB capacity gated (unweighted and duration-weighted)
  2. prefetcher-off fraction
  3. configuration diversity
  4. interval duration
  5. inclusion-violating L3 decisions (applied L3 < sum of active L2)
  6. dynamic vs leakage power, dynamic_rf against max_resources

Which trees, and why (see the DISCREPANCIES section of the summary):
  dynamic_rf 4c/8c  n4_hetero_v2flush        flush, no penalty knobs
  dynamic_rf 16c    n4_hetero_v6_16c_flush   flush, no penalty knobs
  max_resources     n4_hetero_v2 (4c/8c) + n4_hetero_v6_16c_flush (16c)
max_resources is absent from the flush tree because it never requests a shrink, so
the shrink policy cannot affect it; the clamp-tree run is the same experiment.
"""

import argparse
import collections
import csv
import glob
import math
import os
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import sniper_lib
from analyze_final_experiment import (power_windows, parse_mcpat_txt,
                                      processor_power, _prev_cols,
                                      _row_prev_config_tuple)

L2_MAX = 1024 * 1024
L3_MAX = 16384 * 1024
BTB_MAX = 4096

TOPO_SUFFIX = [('_8c_2P6E', '8c 2P+6E'), ('_8c_4P4E', '8c 4P+4E'),
               ('_16c_4P12E', '16c 4P+12E')]
TOPO_ORDER = ['4c 2P+2E', '8c 2P+6E', '8c 4P+4E', '16c 4P+12E']
BENCHMARKS = ['barnes', 'cholesky', 'fft', 'fmm', 'lu.cont', 'lu.ncont',
              'ocean.cont', 'ocean.ncont', 'radiosity', 'radix', 'raytrace',
              'volrend', 'water.nsq', 'water.sp']


def split_topo(dirname):
    for suf, topo in TOPO_SUFFIX:
        if dirname.endswith(suf):
            return dirname[:-len(suf)], topo
    return dirname, '4c 2P+2E'


def geo(xs):
    xs = [x for x in xs if x and x > 0]
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float('nan')


def mean(xs):
    return sum(xs) / float(len(xs)) if xs else float('nan')


def median(xs):
    if not xs:
        return float('nan')
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def load_rows(path, warn):
    """Decision rows with status 'applied'. Anything else is counted and reported."""
    with open(path) as fh:
        all_rows = list(csv.DictReader(fh))
    applied = [r for r in all_rows if (r.get('status') or '').strip() == 'applied']
    if len(applied) != len(all_rows):
        other = collections.Counter((r.get('status') or '').strip()
                                    for r in all_rows if r not in applied)
        warn.append('%s: %d of %d rows not "applied" (%s)' %
                    (path, len(all_rows) - len(applied), len(all_rows), dict(other)))
    return applied


def durations(resultsdir, nrows, warn):
    """Per-interval durations in ns, aligned to decision rows by index.

    power_windows() returns one window per McPAT sample; finalPowerSample() adds a
    closing window at ROI end that has no decision row, so the counts can differ by
    one or two. Truncating to the shorter is correct for per-interval weighting.
    """
    w = [d for (_t0, _t1, d, _p) in power_windows(resultsdir)]
    zeros = sum(1 for d in w if d <= 0)
    if zeros:
        warn.append('%s: %d power windows with zero/negative duration' % (resultsdir, zeros))
    if abs(len(w) - nrows) > 2:
        warn.append('%s: %d power windows vs %d applied decision rows' %
                    (resultsdir, len(w), nrows))
    return w


def run_stats(d, warn):
    dec = os.path.join(d, 'sniper_reconfig_decisions.csv')
    if not os.path.exists(os.path.join(d, 'sim.out')) or not os.path.exists(dec):
        return None
    rows = load_rows(dec, warn)
    if not rows:
        warn.append('%s: no "applied" rows' % d)
        return None

    fields = list(rows[0].keys())
    ncore = len([c for c in fields if c.startswith('l2_bytes_new_core')])
    dur = durations(d, len(rows), warn)
    n = min(len(rows), len(dur)) if dur else len(rows)
    wts = dur[:n] if dur else [1.0] * n

    l2g, l3g, btbg, pfoff, viol = [], [], [], [], []
    for r in rows[:n]:
        l2 = [float(r['l2_bytes_new_core%d' % i]) for i in range(ncore)]
        btb = [float(r['btb_entries_new_core%d' % i]) for i in range(ncore)]
        l3 = float(r['l3_bytes_new'])
        l2g.append(mean([1.0 - v / L2_MAX for v in l2]))
        btbg.append(mean([1.0 - v / BTB_MAX for v in btb]))
        l3g.append(1.0 - l3 / L3_MAX)
        pfoff.append(sum(1 for i in range(ncore)
                         if r['prefetch_new_core%d' % i] == 'none') / float(ncore))
        viol.append(1.0 if l3 < sum(l2) else 0.0)

    def wmean(vals):
        tot = sum(wts)
        return sum(v * w for v, w in zip(vals, wts)) / tot if tot else float('nan')

    # Configuration diversity, using the committed definition in
    # analyze_final_experiment.config_popularity(): the tuple of every '_prev'
    # column, i.e. the configuration actually IN EFFECT during the interval.
    counter = collections.Counter(_row_prev_config_tuple(r, fields) for r in rows[:n])
    top5 = sum(c for _, c in counter.most_common(5))

    return dict(ncore=ncore, nint=n,
                l2_gated=mean(l2g), l2_gated_w=wmean(l2g),
                l3_gated=mean(l3g), l3_gated_w=wmean(l3g),
                btb_gated=mean(btbg), btb_gated_w=wmean(btbg),
                pf_off=mean(pfoff), viol=mean(viol),
                dur_med_us=median(wts) / 1000.0, dur_mean_us=mean(wts) / 1000.0,
                ndistinct=len(counter), top5_frac=float(top5) / n,
                configs=counter)


def power_split(d):
    """(mean dynamic W, mean leakage W), duration-weighted. Leakage = total - dynamic."""
    dyn = leak = wsum = 0.0
    for _t0, _t1, dur, path in power_windows(d):
        dat = parse_mcpat_txt(path)
        if not dat or dur <= 0:
            continue
        pd = processor_power(dat, False)
        pt = processor_power(dat, True)
        dyn += pd * dur
        leak += (pt - pd) * dur
        wsum += dur
    if not wsum:
        return None, None
    return dyn / wsum, leak / wsum


def table(title, header, rows_out):
    print('')
    print('=' * 78)
    print(title)
    print('=' * 78)
    w = [max(len(str(r[i])) for r in [header] + rows_out) for i in range(len(header))]
    line = lambda r: '  '.join(str(c).ljust(w[i]) for i, c in enumerate(r))
    print(line(header))
    print('-' * (sum(w) + 2 * (len(header) - 1)))
    for r in rows_out:
        print(line(r))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--results-root', required=True)
    ap.add_argument('--outdir', default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    R = args.results_root
    DYN_ROOTS = [os.path.join(R, 'n4_hetero_v2flush'),
                 os.path.join(R, 'n4_hetero_v6_16c_flush')]
    MAX_ROOTS = [os.path.join(R, 'n4_hetero_v2'),
                 os.path.join(R, 'n4_hetero_v6_16c_flush')]

    warn = []
    runs = {}          # (bench, topo) -> stats
    missing = []

    for root in DYN_ROOTS:
        for p in sorted(glob.glob(os.path.join(root, '*', 'dynamic_rf', 'sim.out'))):
            dirname = os.path.basename(os.path.dirname(os.path.dirname(p)))
            bench, topo = split_topo(dirname)
            s = run_stats(os.path.dirname(p), warn)
            if s:
                s['root'] = root
                runs[(bench, topo)] = s

    for topo in TOPO_ORDER:
        for b in BENCHMARKS:
            if (b, topo) not in runs:
                missing.append('%s @ %s' % (b, topo))

    by_topo = collections.OrderedDict((t, []) for t in TOPO_ORDER)
    for (b, t), s in runs.items():
        by_topo[t].append(s)

    # ---------------- 1. capacity gated ----------------
    rows_out = []
    for t in TOPO_ORDER:
        rs = by_topo[t]
        if not rs:
            continue
        rows_out.append([t, len(rs)] + ['%.1f%%' % (100 * mean([r[k] for r in rs]))
                         for k in ('l2_gated', 'l2_gated_w', 'l3_gated', 'l3_gated_w',
                                   'btb_gated', 'btb_gated_w')])
    allr = list(runs.values())
    rows_out.append(['All %d' % len(allr), len(allr)] +
                    ['%.1f%%' % (100 * mean([r[k] for r in allr]))
                     for k in ('l2_gated', 'l2_gated_w', 'l3_gated', 'l3_gated_w',
                               'btb_gated', 'btb_gated_w')])
    table('1. Capacity gated (mean fraction of max removed)',
          ['topology', 'runs', 'L2 unw', 'L2 wtd', 'L3 unw', 'L3 wtd', 'BTB unw', 'BTB wtd'],
          rows_out)

    # ---------------- 2. prefetcher off ----------------
    rows_out = []
    for t in TOPO_ORDER + ['ALL']:
        rs = allr if t == 'ALL' else by_topo.get(t, [])
        if not rs:
            continue
        v = [r['pf_off'] for r in rs]
        rows_out.append([t if t != 'ALL' else 'All %d' % len(allr), len(rs),
                         '%.1f%%' % (100 * mean(v)), '%.1f%%' % (100 * min(v)),
                         '%.1f%%' % (100 * median(v)), '%.1f%%' % (100 * max(v)),
                         sum(1 for x in v if x >= 0.94)])
    table('2. Prefetcher off (fraction of core-intervals)',
          ['topology', 'runs', 'mean', 'min', 'median', 'max', 'runs >=94%'], rows_out)

    # ---------------- 3. configuration diversity ----------------
    pooled = collections.Counter()
    for r in allr:
        pooled.update(r['configs'])
    pooled_tot = sum(pooled.values())
    rows_out = []
    for t in TOPO_ORDER + ['ALL']:
        rs = allr if t == 'ALL' else by_topo.get(t, [])
        if not rs:
            continue
        pc = collections.Counter()
        for r in rs:
            pc.update(r['configs'])
        tot = sum(pc.values())
        rows_out.append([t if t != 'ALL' else 'All %d' % len(allr), len(rs),
                         '%.0f' % median([r['ndistinct'] for r in rs]),
                         len(pc),
                         '%.1f%%' % (100 * median([r['top5_frac'] for r in rs])),
                         '%.1f%%' % (100.0 * sum(c for _, c in pc.most_common(5)) / tot)])
    table('3. Configuration diversity  (config = tuple of all "_prev" columns, i.e. the '
          'configuration in effect;\n   definition from analyze_final_experiment.config_popularity)',
          ['topology', 'runs', 'median distinct/run', 'distinct total',
           'top-5 cov (median run)', 'top-5 cov (pooled)'], rows_out)

    # ---------------- 4. interval duration ----------------
    rows_out = []
    for t in TOPO_ORDER + ['ALL']:
        rs = allr if t == 'ALL' else by_topo.get(t, [])
        if not rs:
            continue
        rows_out.append([t if t != 'ALL' else 'All %d' % len(allr), len(rs),
                         '%.1f' % median([r['dur_med_us'] for r in rs]),
                         '%.1f' % mean([r['dur_mean_us'] for r in rs]),
                         '%.1f' % min(r['dur_med_us'] for r in rs),
                         '%.1f' % max(r['dur_med_us'] for r in rs)])
    table('4. Interval duration (us), from McPAT window durations',
          ['topology', 'runs', 'median of run medians', 'mean of run means',
           'min run median', 'max run median'], rows_out)

    # ---------------- 5. inclusion violations ----------------
    rows_out = []
    for t in TOPO_ORDER + ['ALL']:
        rs = allr if t == 'ALL' else by_topo.get(t, [])
        if not rs:
            continue
        v = [r['viol'] for r in rs]
        rows_out.append([t if t != 'ALL' else 'All %d' % len(allr), len(rs),
                         '%.1f%%' % (100 * mean(v)), '%.1f%%' % (100 * max(v))])
    table('5. Inclusion-violating L3 decisions (applied L3 < sum of active L2)',
          ['topology', 'runs', 'mean frac of intervals', 'worst run'], rows_out)
    bar = [[t, '%.1f%%' % (100 * runs[('barnes', t)]['viol'])]
           for t in TOPO_ORDER if ('barnes', t) in runs]
    table('5b. barnes, per topology', ['topology', 'violating intervals'], bar)

    # ---------------- 6. dynamic vs leakage power ----------------
    pw = {}
    for (b, t), s in runs.items():
        dyn_dir = os.path.join(s['root'], b + ''.join(sfx for sfx, tt in TOPO_SUFFIX
                                                      if tt == t), 'dynamic_rf')
        mroot = MAX_ROOTS[1] if t == '16c 4P+12E' else MAX_ROOTS[0]
        max_dir = os.path.join(mroot, b + ''.join(sfx for sfx, tt in TOPO_SUFFIX
                                                  if tt == t), 'max_resources')
        if not os.path.exists(os.path.join(max_dir, 'sim.out')):
            warn.append('no max_resources for %s @ %s (looked in %s)' % (b, t, mroot))
            continue
        ad, al = power_split(dyn_dir)
        md, ml = power_split(max_dir)
        if None in (ad, al, md, ml) or md <= 0 or ml <= 0:
            warn.append('power split unavailable for %s @ %s' % (b, t))
            continue
        pw[(b, t)] = (ad / md, al / ml)

    rows_out = []
    for t in TOPO_ORDER + ['ALL']:
        sel = [v for (b, tt), v in pw.items() if t == 'ALL' or tt == t]
        if not sel:
            continue
        rows_out.append([t if t != 'ALL' else 'All %d' % len(pw), len(sel),
                         '%.3fx' % geo([x[0] for x in sel]),
                         '%.3fx' % geo([x[1] for x in sel]),
                         sum(1 for x in sel if x[0] < 1),
                         sum(1 for x in sel if x[1] < 1)])
    table('6. dynamic_rf / max_resources power (geomean)',
          ['topology', 'pairs', 'dynamic', 'leakage', 'runs lower dyn', 'runs lower leak'],
          rows_out)

    # ---------------- files ----------------
    csv_path = os.path.join(args.outdir, 'paper_stats_results.csv')
    with open(csv_path, 'wb') as fh:
        w = csv.writer(fh)
        w.writerow(['benchmark', 'topology', 'cores', 'intervals',
                    'l2_gated', 'l2_gated_wtd', 'l3_gated', 'l3_gated_wtd',
                    'btb_gated', 'btb_gated_wtd', 'prefetch_off',
                    'distinct_configs', 'top5_coverage',
                    'interval_median_us', 'interval_mean_us',
                    'l3_inclusion_violation_frac',
                    'dyn_power_ratio', 'leak_power_ratio'])
        for (b, t) in sorted(runs, key=lambda k: (TOPO_ORDER.index(k[1]), k[0])):
            s = runs[(b, t)]
            d, l = pw.get((b, t), ('', ''))
            w.writerow([b, t, s['ncore'], s['nint'],
                        '%.4f' % s['l2_gated'], '%.4f' % s['l2_gated_w'],
                        '%.4f' % s['l3_gated'], '%.4f' % s['l3_gated_w'],
                        '%.4f' % s['btb_gated'], '%.4f' % s['btb_gated_w'],
                        '%.4f' % s['pf_off'], s['ndistinct'], '%.4f' % s['top5_frac'],
                        '%.1f' % s['dur_med_us'], '%.1f' % s['dur_mean_us'],
                        '%.4f' % s['viol'],
                        ('%.4f' % d) if d != '' else '', ('%.4f' % l) if l != '' else ''])
    print('')
    print('wrote %s' % csv_path)

    print('')
    print('=' * 78)
    print('CONSISTENCY CHECKS')
    print('=' * 78)
    print('dynamic_rf runs found: %d (expected 56)' % len(runs))
    if missing:
        print('MISSING (%d):' % len(missing))
        for m in missing:
            print('   %s' % m)
    else:
        print('no missing runs')
    print('pooled distinct configurations across all runs: %d over %d intervals'
          % (len(pooled), pooled_tot))
    if warn:
        # Group by kind: 56 identical messages are noise, the count is the signal.
        kinds = collections.Counter()
        for x in warn:
            if 'zero/negative duration' in x:
                kinds['power window with zero/negative duration (the roi-begin->0 '
                      'window; weight 0, no effect on means)'] += 1
            elif 'not "applied"' in x:
                kinds['decision rows with status != applied'] += 1
            elif 'power windows vs' in x:
                kinds['power-window / decision-row count mismatch > 2'] += 1
            else:
                kinds[x] += 1
        print('WARNINGS (%d total):' % len(warn))
        for k, c in kinds.most_common():
            print('   [%d runs] %s' % (c, k))
    else:
        print('no warnings')


if __name__ == '__main__':
    main()
