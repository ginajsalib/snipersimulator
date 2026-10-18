#!/usr/bin/env python3
"""
Plot the four reconfigurable knobs over the length of a run, and branch
misprediction rate, straight from sniper_reconfig_decisions.csv.

  python3 tools/reconfig/plot_knob_trajectories.py \
      --root /home/gina/Desktop/dockerMnt/sniperCodeNewBranch-centos6/results/n4_hetero_v2 \
      --out  tools/reconfig/figures

Produces, per benchmark named with --benchmarks:
  knobs_<bench>.png   four stacked panels sharing the interval axis
and, for every benchmark with both arms:
  mpki_<bench>.png    branch MPKI, dynamic_rf vs max_resources

Why four panels and not one chart: the knobs are in four different units (KB, MB,
entries, percent). Putting two of them on one pair of axes would be a dual-axis
chart, which makes the crossing point an artefact of the scales. Small multiples
on a shared x-axis keep every comparison honest.

Series colours are slots 1-2 of the validated reference palette, used in order.
"""

import argparse
import csv
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

# Reference palette, light mode, slots in fixed order. Not cycled.
C_P      = '#2a78d6'   # slot 1, blue   -> performance cores
C_E      = '#eb6834'   # slot 2, orange -> efficiency cores
INK      = '#0b0b0b'
INK_2    = '#52514e'
GRID     = '#e3e2de'
SURFACE  = '#fcfcfb'


def load(path):
    with open(path, newline='') as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise SystemExit('empty decision log: %s' % path)
    ncore = len([c for c in rows[0] if c.startswith('l2_bytes_new_core')])
    return rows, ncore


def split_cores(ncore):
    """Cores 0..n/2-1 are performance, the rest efficiency (run_n4_hetero.sh)."""
    half = ncore // 2
    return list(range(half)), list(range(half, ncore))


def mean_over(rows, cores, fmt, scale=1.0):
    if not cores:
        return None
    return [sum(float(r[fmt % c]) for c in cores) / len(cores) / scale for r in rows]


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9, length=0)


def plot_knobs(rows, ncore, bench, outpath):
    pc, ec = split_cores(ncore)
    x = list(range(1, len(rows) + 1))

    fig, axes = plt.subplots(4, 1, figsize=(9, 9.2), sharex=True,
                             gridspec_kw={'hspace': 0.42})
    fig.patch.set_facecolor(SURFACE)

    panels = [
        ('L2 capacity per core (KB)', 'l2_bytes_new_core%d', 1024.0, True),
        ('L3 capacity, shared (MB)',  None,                  None,   False),
        ('BTB entries per core',      'btb_entries_new_core%d', 1.0,  True),
        ('L2 prefetcher enabled (% of cores)', None,          None,   False),
    ]

    for ax, (title, fmt, scale, percore) in zip(axes, panels):
        style(ax)
        ax.set_title(title, loc='left', fontsize=10.5, color=INK, pad=6)
        if percore:
            yp = mean_over(rows, pc, fmt, scale)
            ye = mean_over(rows, ec, fmt, scale)
            ax.plot(x, yp, lw=2, color=C_P, label='performance cores', zorder=3)
            ax.plot(x, ye, lw=2, color=C_E, label='efficiency cores', zorder=3)
            # Legend once, on the first panel only; the two series mean the same
            # thing in every panel that has them.
            if ax is axes[0]:
                ax.legend(frameon=False, fontsize=9, labelcolor=INK_2,
                          loc='lower right', ncol=2)
            lo, hi = min(min(yp), min(ye)), max(max(yp), max(ye))
            pad = (hi - lo) * 0.18 or hi * 0.1 or 1
            ax.set_ylim(lo - pad, hi + pad)
        elif title.startswith('L3'):
            y = [float(r['l3_bytes_new']) / 1048576.0 for r in rows]
            ax.plot(x, y, lw=2, color=C_P, zorder=3)
            ax.set_ylim(0, (max(y) * 1.25) or 1)
        else:
            y = [100.0 * sum(1 for c in range(ncore)
                             if r['prefetch_new_core%d' % c] != 'none') / ncore
                 for r in rows]
            ax.plot(x, y, lw=2, color=C_P, zorder=3)
            ax.set_ylim(-4, 104)
        ax.yaxis.set_major_locator(MaxNLocator(4))

    axes[-1].set_xlabel('reconfiguration interval', fontsize=10, color=INK_2)
    fig.text(0.015, 0.975, '%s — applied configuration over the run' % bench,
             ha='left', va='top', fontsize=13.5, color=INK, weight='bold')
    fig.text(0.015, 0.947, '%d intervals, %d cores; values are what was actually '
             'applied, not requested' % (len(rows), ncore),
             ha='left', va='top', fontsize=9.5, color=INK_2)
    fig.subplots_adjust(left=0.10, right=0.975, top=0.885, bottom=0.065)
    fig.savefig(outpath, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print('wrote %s' % outpath)


def plot_mpki(root, bench, outpath):
    series = []
    for arm, colour in (('max_resources', C_P), ('dynamic_rf', C_E)):
        p = os.path.join(root, bench, arm, 'sniper_reconfig_decisions.csv')
        if not os.path.exists(p):
            return False
        rows, ncore = load(p)
        y = [sum(float(r['branch_mpki_core%d' % c]) for c in range(ncore)) / ncore
             for r in rows]
        # Runs differ slightly in interval count, so plot against progress
        # through the run rather than raw index.
        x = [100.0 * i / (len(y) - 1) for i in range(len(y))] if len(y) > 1 else [0]
        series.append((arm, colour, x, y))

    fig, ax = plt.subplots(figsize=(9, 4.0))
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    for arm, colour, x, y in series:
        ax.plot(x, y, lw=2, color=colour, label=arm, zorder=3)
    ax.set_xlabel('progress through run (%)', fontsize=10, color=INK_2)
    ax.set_ylabel('branch MPKI (mean over cores)', fontsize=10, color=INK_2)
    ax.legend(frameon=False, fontsize=9.5, labelcolor=INK_2)
    ax.set_title('%s — branch mispredictions per 1000 instructions' % bench,
                 loc='left', fontsize=13, color=INK, weight='bold', pad=10)
    fig.tight_layout()
    fig.savefig(outpath, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    print('wrote %s' % outpath)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', required=True)
    ap.add_argument('--benchmarks', default='water.sp barnes')
    ap.add_argument('--arm', default='dynamic_rf')
    ap.add_argument('--out', default='.')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    for bench in args.benchmarks.split():
        p = os.path.join(args.root, bench, args.arm, 'sniper_reconfig_decisions.csv')
        if not os.path.exists(p):
            print('skip %s (no %s)' % (bench, p), file=sys.stderr)
            continue
        rows, ncore = load(p)
        plot_knobs(rows, ncore, bench,
                   os.path.join(args.out, 'knobs_%s.png' % bench))
        plot_mpki(args.root, bench, os.path.join(args.out, 'mpki_%s.png' % bench))


if __name__ == '__main__':
    main()
