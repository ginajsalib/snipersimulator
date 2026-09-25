#!/usr/bin/env python3
"""Enumerate the sweep configurations that were never simulated, per benchmark.

Reads the existing merged_full_<bench>.csv sweep data, diffs it against the full
factorial grid, and writes the missing points as a TSV that run_missing_sweep.sh
consumes inside the container.

The grid is 3 L2 x 3 L2 x 3 L3 x 2 PF x 2 PF x 4 BTB x 4 BTB = 1728 per benchmark.
Coverage today is 1056/1728 for barnes/cholesky/fft and 672/1728 for radiosity, with
two systematic holes that are the same in every benchmark:
  * L2 = (256,256) absent at L3 = 4096 and 16384
  * L3 = 8192 present ONLY for L2 = (256,256)
i.e. two sweeps that never overlapped. The first hole matters most: it is exactly the
region the runtime model operates in, so offline scoring substituted near-oracle PPW
there while best_static (chosen from the sweep) always had real measurements.

Usage:
  gen_missing_configs.py --merged-full-dir <dir> --out <file.tsv> [--tier all|symmetric|l3-8192]
"""
import argparse, itertools, os, sys

L2_VALS  = [256, 512, 1024]
L3_VALS  = [4096, 8192, 16384]
BTB_VALS = [512, 1024, 2048, 4096]
PF_VALS  = ['none', 'simple']
BENCHES  = ['barnes', 'cholesky', 'fft', 'radiosity']
COLS = ['L2 core 0','L2 core 1','L3','Prefetch core 0','Prefetch core 1','BTB core 0','BTB core 1']


def swept_configs(path):
    import pandas as pd
    seen = set()
    for chunk in pd.read_csv(path, usecols=COLS, chunksize=500000, low_memory=False):
        for row in chunk[COLS].dropna().values.tolist():
            seen.add((int(row[0]), int(row[1]), int(row[2]),
                      str(row[3]).strip(), str(row[4]).strip(),
                      int(row[5]), int(row[6])))
    return seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--merged-full-dir', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--tier', default='all', choices=['all', 'symmetric', 'l3-8192'],
                    help="symmetric: only the L2=(256,256) hole at L3 4096/16384 -- the "
                         "region the runtime model actually occupies. l3-8192: fill the "
                         "middle L3 size. all: everything.")
    ap.add_argument('--benchmarks', default=' '.join(BENCHES))
    args = ap.parse_args()

    grid = set(itertools.product(L2_VALS, L2_VALS, L3_VALS, PF_VALS, PF_VALS,
                                 BTB_VALS, BTB_VALS))
    rows, summary = [], []
    for bench in args.benchmarks.split():
        path = os.path.join(args.merged_full_dir, 'merged_full_%s.csv' % bench)
        if not os.path.exists(path):
            print('  %-10s SKIP (no %s)' % (bench, path), file=sys.stderr)
            continue
        seen = {c for c in swept_configs(path) if c[3] in PF_VALS and c[4] in PF_VALS}
        missing = grid - seen
        if args.tier == 'symmetric':
            missing = {m for m in missing if m[2] != 8192}
        elif args.tier == 'l3-8192':
            missing = {m for m in missing if m[2] == 8192}
        for m in sorted(missing):
            rows.append((bench,) + m)
        summary.append((bench, len(seen), len(grid), len(missing)))

    with open(args.out, 'w') as f:
        f.write('# benchmark\tl2_core0\tl2_core1\tl3\tpf_core0\tpf_core1\tbtb_core0\tbtb_core1\n')
        for r in rows:
            f.write('\t'.join(str(x) for x in r) + '\n')

    print('tier: %s' % args.tier, file=sys.stderr)
    for bench, nseen, ngrid, nmiss in summary:
        print('  %-10s swept %4d/%d (%.1f%%)  ->  %4d to run'
              % (bench, nseen, ngrid, 100.0 * nseen / ngrid, nmiss), file=sys.stderr)
    print('  TOTAL %d runs  (~%.1f days serial at 30 min/run)'
          % (len(rows), len(rows) * 30 / 1440.0), file=sys.stderr)
    print('  wrote %s' % args.out, file=sys.stderr)


if __name__ == '__main__':
    main()
