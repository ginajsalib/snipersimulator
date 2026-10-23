#!/usr/bin/env python2
"""
Per-benchmark CSV for one topology tree: throughput, power (dynamic and total),
energy, efficiency and PPW for both arms, plus the instruction-mix check that says
whether those numbers mean anything.

  python2 tools/reconfig/summarize_topology.py \
      --root .../results/n4_hetero_v6_16c_flush --suffix _16c_4P12E \
      --csv ~/results_16c.csv

The mix check exists because every run stops at a fixed instruction count
(stop-by-icount). A benchmark whose threads spin at barriers burns that budget on
spin-loop instructions, which inflates IPS for whichever arm spins more -- and that
is systematically the SLOWER arm. Where the arms' instruction mixes differ, IPS,
energy and PPW are not comparable and the work-rate columns should be used instead.
"""

import argparse
import csv
import glob
import math
import os
import sys

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import sniper_lib
from analyze_final_experiment import power_windows, parse_mcpat_txt, processor_power

UOPS = ('branch', 'load', 'store', 'fp_addsub', 'fp_muldiv', 'generic')


def total(r, k):
    v = r.get(k)
    return (sum(v) if isinstance(v, list) else v) or 0


def measure(d):
    if not os.path.exists(os.path.join(d, 'sim.out')):
        return None
    r = sniper_lib.get_results(resultsdir=d)['results']
    instrs = total(r, 'performance_model.instruction_count')
    cycles = total(r, 'performance_model.cycle_count')
    elapsed = max(r['performance_model.elapsed_time']) / 1e15
    if not instrs or not elapsed:
        return None

    u = dict((n, total(r, 'rob_timer.uop_' + n)) for n in UOPS)
    utot = sum(u.values()) or 1

    dyn = leak = wsum = 0.0
    for t0, t1, dur, path in power_windows(d):
        dat = parse_mcpat_txt(path)
        if not dat:
            continue
        dyn += processor_power(dat, False) * dur
        leak += processor_power(dat, True) * dur
        wsum += dur
    if not wsum:
        return None
    w_dyn, w_tot = dyn / wsum, leak / wsum
    ips = instrs / elapsed

    return dict(
        instrs=instrs, elapsed=elapsed, ips=ips,
        ipc=float(instrs) / cycles if cycles else 0,
        w_dyn=w_dyn, w_tot=w_tot,
        e_dyn=w_dyn * elapsed, e_tot=w_tot * elapsed,
        ipw_dyn=ips / w_dyn, ipw_tot=ips / w_tot,
        ppw_dyn=ips ** 3 / w_dyn, ppw_tot=ips ** 3 / w_tot,
        br_pct=100.0 * u['branch'] / utot,
        st_pct=100.0 * u['store'] / utot,
        store=u['store'], fp=u['fp_addsub'] + u['fp_muldiv'],
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True)
    ap.add_argument('--suffix', default='', help='topology suffix on directory names')
    ap.add_argument('--csv', required=True)
    ap.add_argument('--mix-threshold', type=float, default=1.0,
                    help='branch-fraction difference (pp) above which a cell is '
                         'flagged as spin-contaminated')
    args = ap.parse_args()

    cols = ['benchmark',
            'ips_max', 'ips_dyn', 'ips_ratio',
            'w_dyn_max', 'w_dyn_dyn', 'w_dyn_ratio',
            'w_tot_max', 'w_tot_dyn', 'w_tot_ratio',
            'e_tot_max', 'e_tot_dyn', 'e_tot_ratio',
            'ipw_tot_max', 'ipw_tot_dyn', 'ipw_tot_ratio',
            'ppw_tot_max', 'ppw_tot_dyn', 'ppw_tot_ratio',
            'ipc_max', 'ipc_dyn',
            'branch_pct_max', 'branch_pct_dyn', 'store_pct_max', 'store_pct_dyn',
            'mix_ok', 'storerate_ratio', 'fprate_ratio']
    rows = []

    for p in sorted(glob.glob(os.path.join(args.root, '*', 'dynamic_rf', 'sim.out'))):
        dirname = os.path.basename(os.path.dirname(os.path.dirname(p)))
        bench = dirname[:-len(args.suffix)] if args.suffix and dirname.endswith(args.suffix) else dirname
        a = measure(os.path.dirname(p))
        m = measure(os.path.join(args.root, dirname, 'max_resources'))
        if not a or not m:
            continue
        mix_ok = abs(a['br_pct'] - m['br_pct']) <= args.mix_threshold
        sr = (a['store'] / a['elapsed']) / (m['store'] / m['elapsed']) if m['store'] else ''
        fr = (a['fp'] / a['elapsed']) / (m['fp'] / m['elapsed']) if m['fp'] > 1000 else ''
        rows.append({
            'benchmark': bench,
            'ips_max': '%.6g' % m['ips'], 'ips_dyn': '%.6g' % a['ips'],
            'ips_ratio': '%.4f' % (a['ips'] / m['ips']),
            'w_dyn_max': '%.3f' % m['w_dyn'], 'w_dyn_dyn': '%.3f' % a['w_dyn'],
            'w_dyn_ratio': '%.4f' % (a['w_dyn'] / m['w_dyn']),
            'w_tot_max': '%.3f' % m['w_tot'], 'w_tot_dyn': '%.3f' % a['w_tot'],
            'w_tot_ratio': '%.4f' % (a['w_tot'] / m['w_tot']),
            'e_tot_max': '%.4f' % m['e_tot'], 'e_tot_dyn': '%.4f' % a['e_tot'],
            'e_tot_ratio': '%.4f' % (a['e_tot'] / m['e_tot']),
            'ipw_tot_max': '%.6g' % m['ipw_tot'], 'ipw_tot_dyn': '%.6g' % a['ipw_tot'],
            'ipw_tot_ratio': '%.4f' % (a['ipw_tot'] / m['ipw_tot']),
            'ppw_tot_max': '%.6g' % m['ppw_tot'], 'ppw_tot_dyn': '%.6g' % a['ppw_tot'],
            'ppw_tot_ratio': '%.4f' % (a['ppw_tot'] / m['ppw_tot']),
            'ipc_max': '%.4f' % m['ipc'], 'ipc_dyn': '%.4f' % a['ipc'],
            'branch_pct_max': '%.1f' % m['br_pct'], 'branch_pct_dyn': '%.1f' % a['br_pct'],
            'store_pct_max': '%.1f' % m['st_pct'], 'store_pct_dyn': '%.1f' % a['st_pct'],
            'mix_ok': 'yes' if mix_ok else 'NO-spin-contaminated',
            'storerate_ratio': ('%.4f' % sr) if sr != '' else '',
            'fprate_ratio': ('%.4f' % fr) if fr != '' else '',
        })

    if not rows:
        sys.exit('no complete (dynamic_rf, max_resources) pairs under %s' % args.root)

    with open(args.csv, 'wb') as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    clean = [r for r in rows if r['mix_ok'] == 'yes']
    geo = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs))
    print('wrote %s  (%d benchmarks, %d mix-clean)' % (args.csv, len(rows), len(clean)))
    for label, key in (('IPS', 'ips_ratio'), ('W total', 'w_tot_ratio'),
                       ('E total', 'e_tot_ratio'), ('IPS/W total', 'ipw_tot_ratio'),
                       ('PPW total', 'ppw_tot_ratio')):
        allv = geo([float(r[key]) for r in rows])
        clv = geo([float(r[key]) for r in clean]) if clean else float('nan')
        print('  %-12s geomean all %7.3fx   mix-clean only %7.3fx' % (label, allv, clv))


if __name__ == '__main__':
    main()
