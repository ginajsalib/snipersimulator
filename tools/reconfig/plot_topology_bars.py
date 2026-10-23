#!/usr/bin/env python3
"""
Grouped bar comparison of max_resources vs dynamic_rf for one topology, from the
CSV written by summarize_topology.py.

  python3 tools/reconfig/plot_topology_bars.py --csv ~/results_16c_4P12E.csv \
      --title "16 cores, 4P+12E, flush" --out tools/reconfig/figures_16c

Three panels, one per measure, sharing the benchmark axis. Three panels and not one
chart because the measures are in different units -- a shared axis would make any
crossing an artefact of the scales, and a second y-axis would be worse.

Scales are linear and each bar carries its value, because IPS and IPS/W span ~40x
across benchmarks: a log axis would misrepresent bar length (bars encode magnitude
from zero), and on a linear axis the smallest bars are slivers, so the number has to
be legible on its own.

Benchmarks whose two arms have different instruction mixes are marked with * -- for
those, IPS is inflated for whichever arm spins more at barriers and the comparison
is not valid. See summarize_topology.py.
"""

import argparse
import csv
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Reference palette, light mode, slots 1-2 in fixed order.
C_MAX = '#2a78d6'   # slot 1, blue   -> max_resources
C_DYN = '#eb6834'   # slot 2, orange -> dynamic_rf
INK, INK_2, GRID, SURFACE = '#0b0b0b', '#52514e', '#e3e2de', '#fcfcfb'

PANELS = [
    ('Total power (W)  — lower is better', 'w_tot_max', 'w_tot_dyn', '%.0f'),
    ('Throughput, IPS  — higher is better', 'ips_max', 'ips_dyn', '%.2g'),
    ('IPS per total watt  — higher is better', 'ipw_tot_max', 'ipw_tot_dyn', '%.2g'),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--out', default='.')
    ap.add_argument('--title', default='')
    ap.add_argument('--sort-by', default='ipw_tot_ratio')
    args = ap.parse_args()

    with open(args.csv) as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: float(r[args.sort_by]))

    labels = [r['benchmark'] + ('' if r['mix_ok'] == 'yes' else ' *') for r in rows]
    y = list(range(len(rows)))
    h = 0.38                      # leaves a visible gap between the paired bars

    os.makedirs(args.out, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(14.5, 6.6), sharey=True)
    fig.patch.set_facecolor(SURFACE)

    for ax, (title, kmax, kdyn, fmt) in zip(axes, PANELS):
        vmax = [float(r[kmax]) for r in rows]
        vdyn = [float(r[kdyn]) for r in rows]
        ax.barh([v + h / 2 for v in y], vmax, height=h, color=C_MAX,
                label='max_resources', zorder=3)
        ax.barh([v - h / 2 for v in y], vdyn, height=h, color=C_DYN,
                label='dynamic_rf', zorder=3)

        lim = max(vmax + vdyn) * 1.30
        ax.set_xlim(0, lim)
        for yy, v in zip(y, vmax):
            ax.text(v + lim * 0.015, yy + h / 2, fmt % v, va='center',
                    fontsize=7.5, color=INK_2)
        for yy, v in zip(y, vdyn):
            ax.text(v + lim * 0.015, yy - h / 2, fmt % v, va='center',
                    fontsize=7.5, color=INK_2)

        ax.set_facecolor(SURFACE)
        ax.grid(True, axis='x', color=GRID, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for s in ('top', 'right', 'left'):
            ax.spines[s].set_visible(False)
        ax.spines['bottom'].set_color(GRID)
        ax.tick_params(colors=INK_2, labelsize=9, length=0)
        ax.set_title(title, loc='left', fontsize=10.5, color=INK, pad=8)

    axes[0].set_yticks(y)
    axes[0].set_yticklabels(labels, fontsize=9.5, color=INK)
    axes[0].set_ylim(-0.6, len(rows) - 0.4)
    # Figure-level, clear of the bars: the panels are full of value labels, so any
    # in-axes placement lands on one.
    handles, lbls = axes[0].get_legend_handles_labels()
    fig.legend(handles, lbls, frameon=False, fontsize=10, labelcolor=INK_2,
               ncol=2, loc='upper right', bbox_to_anchor=(0.985, 0.995))

    if args.title:
        fig.text(0.008, 0.975, args.title, ha='left', va='top',
                 fontsize=13.5, color=INK, weight='bold')
    fig.text(0.008, 0.035,
             '* instruction mix differs between the arms: these runs stop at a fixed '
             'instruction count, so spin-waiting at barriers inflates IPS for whichever '
             'arm spins more. IPS and IPS/W are not comparable for these four.',
             ha='left', va='top', fontsize=8.5, color=INK_2)
    fig.subplots_adjust(left=0.085, right=0.985, top=0.90, bottom=0.11, wspace=0.14)

    out = os.path.join(args.out, 'bars_%s.png' %
                       os.path.splitext(os.path.basename(args.csv))[0])
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print('wrote %s' % out)


if __name__ == '__main__':
    main()
