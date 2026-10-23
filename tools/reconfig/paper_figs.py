#!/usr/bin/env python3
"""Vector (PDF) figures for the paper: fft cache usage and barnes branch mispredictions.

Usage: paper_figs.py [DATA_DIR] [OUT_DIR]
  DATA_DIR defaults to ~/Desktop/FinalTrainingJuly (holds <bench>/merged_full_<bench>.csv)
"""
import os
import re
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Desktop/FinalTrainingJuly")
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else os.path.join(DATA_DIR, "paper_figs")

plt.rcParams.update({
    "pdf.fonttype": 42,          # embed TrueType, no Type 3 fonts
    "ps.fonttype": 42,
    "font.family": "serif",
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "axes.linewidth": 0.6,
    "lines.linewidth": 0.8,
    "grid.linewidth": 0.4,
})
# Distinguishable in grayscale: dark solid + circles vs light dashed + triangles.
STYLES = {
    0: dict(color="#000000", linestyle="-", marker="o", markerfacecolor="#000000"),
    1: dict(color="#8c8c8c", linestyle=(0, (4, 1.5)), marker="^", markerfacecolor="white"),
}
WIDTH = 7.0  # in; full text width of a two-column page


def load(bench, l2, l3_kb, btb, pf):
    df = pd.read_csv(os.path.join(DATA_DIR, bench, f"merged_full_{bench}.csv"))
    sel = df[(df.l2Core0 == l2[0]) & (df.l2Core1 == l2[1]) & (df.l3Size == l3_kb)
             & (df.btbCore0 == btb[0]) & (df.btbCore1 == btb[1])
             & (df.prefetchCore0 == pf[0]) & (df.prefetchCore1 == pf[1])]
    # Keep the main interval run; drop stray rows from other run directories.
    sel = sel[sel.directory_perf.str.startswith("/export/")]
    start = sel.period_start.map(lambda s: 0 if s == "roi-begin" else int(re.search(r"(\d+)$", s).group(1)))
    sel = sel.assign(_start=start).sort_values("_start").reset_index(drop=True)
    if sel.empty:
        sys.exit(f"no rows for {bench} config")
    return sel


def load_time_run(bench, config_dir):
    """Older time-based interval run (/root/...); it only survives in cleaned_perf, not merged_full."""
    df = pd.read_csv(os.path.join(DATA_DIR, bench, f"cleaned_perf_{bench}.csv"))
    sel = df[df.directory == f"/root/snipersim_framework/{config_dir}"].reset_index(drop=True)
    if sel.empty:
        sys.exit(f"no time-based rows for {bench} {config_dir}")
    return sel


def plot_cores(ax, df, col, ylabel, cores=(0, 1), legend_loc="upper center"):
    every = max(1, len(df) // 40)
    for core in cores:
        ax.plot(df.index, df[f"{col}_core{core}"], label=f"Core {core}",
                markersize=3, markeredgewidth=0.6, markevery=(core * every // 2, every),
                **STYLES[core])
    ax.set_ylim(top=ax.get_ylim()[1] * 1.15)  # headroom so the legend clears the peaks
    ax.set_ylabel(ylabel)
    ax.grid(True, color="#d9d9d9")
    ax.set_axisbelow(True)
    ax.margins(x=0.01)
    ax.legend(loc=legend_loc, frameon=True, framealpha=1, edgecolor="none", ncol=len(cores))


def plot_mispred(df, xlabel, name, core=0):
    fig, ax = plt.subplots(figsize=(WIDTH, 1.6))
    ax.plot(df.index, df[f"branch_mispred_rate_core{core}"], color="black")
    ax.set_ylabel("Mispred. rate")
    ax.set_xlabel(xlabel)
    ax.grid(True, color="#d9d9d9")
    ax.set_axisbelow(True)
    ax.margins(x=0.01)
    fig.tight_layout(pad=0.3)
    fig.savefig(os.path.join(OUT_DIR, name), bbox_inches="tight", pad_inches=0.01)
    plt.close(fig)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    fft = load("fft", (1024, 256), 4096, (2048, 1024), ("none", "none"))
    fig, ax = plt.subplots(figsize=(WIDTH, 1.7))
    plot_cores(ax, fft, "l2_most_usage", "L2 usage (lines)")
    ax.set_xlabel("Execution interval (1M instructions)")
    fig.tight_layout(pad=0.3)
    fig.savefig(os.path.join(OUT_DIR, "fft_cache_behavior.pdf"), bbox_inches="tight", pad_inches=0.01)
    plt.close(fig)

    barnes = load_time_run("barnes", "config_l2_256_l3MB_8192_prefetch_none_branch_512-512_barnes-intervals")
    plot_mispred(barnes, "Execution interval", "barnes_branch_mispred.pdf")

    print(f"fft: {len(fft)} intervals, barnes (time-based): {len(barnes)} intervals -> {OUT_DIR}/")


if __name__ == "__main__":
    main()
