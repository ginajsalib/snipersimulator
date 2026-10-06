#!/usr/bin/env python2
"""
Compare dynamic_rf under shrink_policy=flush against dynamic_rf under clamp and against
the max_resources static baseline, for every (benchmark, topology) present in both trees.

Run on the HOST (python2, needs tools/sniper_lib -- same environment tools/mcpat.py uses).

  python2 tools/reconfig/compare_shrink_policies.py \
    --clamp-root /home/gina/Desktop/dockerMnt/.../results/n4_hetero \
    --flush-root /home/gina/Desktop/dockerMnt/.../results/n4_hetero_flush

max_resources is taken from the clamp tree: it never requests a shrink, so the policy
cannot affect it and re-running it under flush would produce an identical arm.

Metrics, all measured (no surrogate):
  IPC      sum(performance_model.instruction_count) / sum(performance_model.cycle_count)
           -- cycle_count is already per-core-frequency, so this is correct for a
           heterogeneous machine where a plain instructions/time would not be.
  hit rate 1 - (load-misses + store-misses) / (loads + stores), per cache level.
           Demand accesses only: reconfig-flush-reads/-writebacks are deliberately NOT
           folded in here (tools/mcpat.py folds them into the power model instead), so a
           flush never flatters its own hit rate.
  IPS      total instructions / elapsed seconds
  W dyn    McPAT Processor "Runtime Dynamic" over the run's power-*.txt samples
  W tot    dynamic + "Subthreshold Leakage with power gating" + "Gate Leakage".
           This is the one to read for a gating study. Way-gating shrinks the array
           McPAT is handed (writeLiveConfigSnapshot writes cache_size AND associativity
           from getActiveWays(), and mcpat.py sets power_gating=1), so the saving lands
           entirely in leakage -- a dynamic-only figure cannot show it, and in fact moves
           the wrong way, because a config that stops stalling retires more instructions
           per second and so switches more. Measured over 42 cells, flush vs
           max_resources: W dyn 1.99x (worse), leakage alone 0.63x (better in 42/42),
           W tot 0.91x.
  energy   power * elapsed -- the per-job cost. Prefer this over power: the arms differ
           in runtime by up to 20x, and power is a rate.
  IPS/W    on both power figures.
  PPW      ips**3 / power, on both. The dynamic-only form is the project/training
           definition and is kept for continuity, but note it structurally cannot reward
           power gating: gating moves power out of the only term the formula contains.
"""

import argparse
import csv
import glob
import os
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import sniper_lib

from analyze_final_experiment import (
    power_windows, parse_mcpat_txt, processor_power, compute_ppw,
)

TOPO_SUFFIXES = (('_8c_2P6E', '8c 2P+6E'), ('_8c_4P4E', '8c 4P+4E'))
LEVELS = ('L1-D', 'L2', 'L3')


def split_topology(dirname):
    for suf, topo in TOPO_SUFFIXES:
        if dirname.endswith(suf):
            return dirname[:-len(suf)], topo
    return dirname, '4c 2P+2E'


def _total(results, key):
    v = results.get(key)
    if v is None:
        return None
    return sum(v) if isinstance(v, list) else v


def hit_rates(results):
    """Demand hit rate per level, or None where the level reported no accesses."""
    out = {}
    for lvl in LEVELS:
        loads = _total(results, '%s.loads' % lvl) or 0
        stores = _total(results, '%s.stores' % lvl) or 0
        lm = _total(results, '%s.load-misses' % lvl) or 0
        sm = _total(results, '%s.store-misses' % lvl) or 0
        acc = loads + stores
        out[lvl] = (1.0 - float(lm + sm) / acc) if acc else None
    return out


def whole_run_power(resultsdir):
    """Time-weighted mean of the per-interval McPAT samples, dynamic and +leakage."""
    # power_windows() is find_power_files() with each duration taken from the stats
    # snapshots rather than the filename -- the closing window's filename duration is
    # inflated 2-3x, because it subtracts core 0's frozen clock from a live one. See
    # analyze_final_experiment.snapshot_times_ns().
    files = power_windows(resultsdir)
    if not files:
        return None, None
    dyn, leak, wsum = 0.0, 0.0, 0.0
    for t0, t1, dur_ns, path in files:
        dat = parse_mcpat_txt(path)
        if not dat:
            continue
        w = max(1, dur_ns)
        dyn += processor_power(dat, False) * w
        leak += processor_power(dat, True) * w
        wsum += w
    if not wsum:
        return None, None
    return dyn / wsum, leak / wsum


def flush_volume(resultsdir):
    """Lines evicted by shrink_policy=flush, from the per-level counters."""
    try:
        r = sniper_lib.get_results(resultsdir=resultsdir)['results']
    except Exception:
        return None
    n = 0
    for lvl in LEVELS:
        n += _total(r, '%s.reconfig-flush-reads' % lvl) or 0
    return n


def realized_shrink(resultsdir):
    """Time-averaged active L2 (per core) and L3 capacity, as % below the max config."""
    f = os.path.join(resultsdir, 'sniper_reconfig_decisions.csv')
    if not os.path.exists(f):
        return None, None
    rows = list(csv.DictReader(open(f)))
    if not rows:
        return None, None
    ncore = len([c for c in rows[0] if c.startswith('l2_bytes_req_core')])
    if not ncore:
        return None, None
    l2 = sum(sum(float(r['l2_bytes_new_core%d' % i]) for i in range(ncore))
             for r in rows) / len(rows) / ncore
    l3 = sum(float(r['l3_bytes_new']) for r in rows) / len(rows)
    return 100.0 * (1 - l2 / 1048576.0), 100.0 * (1 - l3 / 16777216.0)


def measure(resultsdir):
    if not os.path.exists(os.path.join(resultsdir, 'sim.out')):
        return None
    res = sniper_lib.get_results(resultsdir=resultsdir)['results']
    instrs = _total(res, 'performance_model.instruction_count')
    cycles = _total(res, 'performance_model.cycle_count')
    elapsed_fs = max(res['performance_model.elapsed_time'])
    power, power_leak = whole_run_power(resultsdir)
    if not instrs or not power:
        return None
    ips, ppw = compute_ppw(instrs, elapsed_fs, power)
    t = elapsed_fs / 1e15
    m = dict(instrs=instrs, ipc=(float(instrs) / cycles if cycles else None),
             t=t, ips=ips, power=power, power_leak=power_leak,
             energy=power * t, energy_leak=power_leak * t, ppw=ppw,
             # Leakage-inclusive forms. power_leak is already dynamic+leakage, so it is
             # the total power; leak_only is broken out because it is the term the
             # gating actually acts on.
             leak_only=power_leak - power,
             ips_per_w=(ips / power if power else None),
             ips_per_w_tot=(ips / power_leak if power_leak else None),
             ppw_tot=((ips ** 3) / power_leak if power_leak else None))
    m.update(hit_rates(res))
    m['flush_lines'] = flush_volume(resultsdir)
    m['l2_got'], m['l3_got'] = realized_shrink(resultsdir)
    return m


def pct(a, b):
    """a relative to b, as a ratio string."""
    if a is None or b is None or b == 0:
        return '--'
    return '%.2fx' % (a / b)


def fmt_hr(x):
    return '--' if x is None else '%.1f%%' % (100.0 * x)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--clamp-root', required=True,
                   help='results root built with shrink_policy=clamp (also supplies max_resources)')
    p.add_argument('--flush-root', required=True,
                   help='results root built with shrink_policy=flush')
    p.add_argument('--flush-priced-root', default=None,
                   help='optional third root: shrink_policy=flush with non-zero '
                        'reconfig/flush_penalty_cycles_* (a realistic DRAM write price)')
    p.add_argument('--benchmarks', default=None,
                   help='space-separated subset; default: everything present in both roots')
    p.add_argument('--csv', default=None, help='also write the full table here')
    args = p.parse_args()

    # The marker records the policy AND the flush prices, so an unpriced and a priced
    # flush tree are distinguishable -- they would otherwise look identical here.
    for root, want in ((args.clamp_root, 'clamp'), (args.flush_root, 'flush')):
        marker = os.path.join(root, 'shrink_policy.txt')
        if os.path.exists(marker):
            got = open(marker).read().strip()
            if not got.startswith(want):
                sys.exit('ERROR: %s is a "%s" tree, expected %s' % (root, got, want))
    if args.flush_priced_root:
        marker = os.path.join(args.flush_priced_root, 'shrink_policy.txt')
        if os.path.exists(marker):
            got = open(marker).read().strip()
            if 'dirty_llc=0' in got and 'line=0' in got:
                sys.exit('ERROR: %s has no flush price set (%s); it is the same experiment '
                         'as --flush-root' % (args.flush_priced_root, got))

    wanted = set(args.benchmarks.split()) if args.benchmarks else None
    cells = []
    for d in sorted(glob.glob(os.path.join(args.flush_root, '*', 'dynamic_rf'))):
        dirname = os.path.basename(os.path.dirname(d))
        bench, topo = split_topology(dirname)
        if wanted and bench not in wanted:
            continue
        arms = {
            'flush': d,
            'clamp': os.path.join(args.clamp_root, dirname, 'dynamic_rf'),
            'max': os.path.join(args.clamp_root, dirname, 'max_resources'),
        }
        if args.flush_priced_root:
            arms['priced'] = os.path.join(args.flush_priced_root, dirname, 'dynamic_rf')
        got = {}
        for k, path in arms.items():
            m = measure(path)
            if m:
                got[k] = m
        if 'flush' in got and 'clamp' in got:
            cells.append((bench, topo, got))

    if not cells:
        sys.exit('No (benchmark, topology) had both a flush and a clamp dynamic_rf run.')

    hdr = ['benchmark', 'topology', 'arm', 'IPC', 'L1-D hit', 'L2 hit', 'L3 hit',
           'IPS', 'W dyn', 'W leak', 'W tot', 'E dyn (J)', 'E tot (J)',
           'IPS/W dyn', 'IPS/W tot', 'PPW dyn', 'PPW tot', 'L2 got', 'L3 got',
           'flush lines']
    rows = []
    for bench, topo, got in cells:
        for arm in ('max', 'clamp', 'flush', 'priced'):
            m = got.get(arm)
            if not m:
                continue
            rows.append([
                bench, topo, {'max': 'max_resources', 'clamp': 'dyn_rf clamp',
                              'flush': 'dyn_rf flush', 'priced': 'dyn_rf flush+DRAM'}[arm],
                '%.3f' % m['ipc'] if m['ipc'] else '--',
                fmt_hr(m['L1-D']), fmt_hr(m['L2']), fmt_hr(m['L3']),
                '%.3g' % m['ips'], '%.1f' % m['power'], '%.1f' % m['leak_only'],
                '%.1f' % m['power_leak'],
                '%.3f' % m['energy'], '%.3f' % m['energy_leak'],
                '%.3g' % m['ips_per_w'], '%.3g' % m['ips_per_w_tot'],
                '%.3g' % m['ppw'], '%.3g' % m['ppw_tot'],
                '--' if m['l2_got'] is None else '%.0f%%' % m['l2_got'],
                '--' if m['l3_got'] is None else '%.0f%%' % m['l3_got'],
                '--' if not m['flush_lines'] else '%d' % m['flush_lines'],
            ])

    widths = [max(len(str(r[i])) for r in [hdr] + rows) for i in range(len(hdr))]
    line = lambda r: '  '.join(str(c).ljust(widths[i]) for i, c in enumerate(r))
    print(line(hdr))
    print('-' * (sum(widths) + 2 * (len(hdr) - 1)))
    last = None
    for r in rows:
        if last is not None and (r[0], r[1]) != last:
            print('')
        print(line(r))
        last = (r[0], r[1])

    print('')
    print('=' * 70)
    print('flush vs clamp (dynamic_rf), and each vs max_resources')
    print('=' * 70)
    h2 = ['benchmark', 'topology', 'IPC f/c', 'L2hit f-c', 'IPS f/c',
          'Wtot f/c', 'Etot f/c', 'PPWtot f/c',
          'PPWtot clamp/max', 'PPWtot flush/max', 'Etot priced/f', 'PPWtot priced/max']
    r2 = []
    for bench, topo, got in cells:
        f, c, mx = got['flush'], got['clamp'], got.get('max')
        dl2 = ('%+.1f pp' % (100.0 * (f['L2'] - c['L2']))) \
            if (f['L2'] is not None and c['L2'] is not None) else '--'
        pr = got.get('priced')
        # Ratios are on the leakage-inclusive figures: those are what a gating change
        # moves. The dynamic-only columns stay in the per-arm table above.
        r2.append([bench, topo, pct(f['ipc'], c['ipc']), dl2, pct(f['ips'], c['ips']),
                   pct(f['power_leak'], c['power_leak']),
                   pct(f['energy_leak'], c['energy_leak']),
                   pct(f['ppw_tot'], c['ppw_tot']),
                   pct(c['ppw_tot'], mx['ppw_tot']) if mx else '--',
                   pct(f['ppw_tot'], mx['ppw_tot']) if mx else '--',
                   pct(pr['energy_leak'], f['energy_leak']) if pr else '--',
                   pct(pr['ppw_tot'], mx['ppw_tot']) if (pr and mx) else '--'])
    w2 = [max(len(str(r[i])) for r in [h2] + r2) for i in range(len(h2))]
    l2 = lambda r: '  '.join(str(c).ljust(w2[i]) for i, c in enumerate(r))
    print(l2(h2))
    print('-' * (sum(w2) + 2 * (len(h2) - 1)))
    for r in r2:
        print(l2(r))

    if args.csv:
        with open(args.csv, 'wb') as fh:
            w = csv.writer(fh)
            w.writerow(hdr)
            w.writerows(rows)
        print('')
        print('full table -> %s' % args.csv)


if __name__ == '__main__':
    main()
