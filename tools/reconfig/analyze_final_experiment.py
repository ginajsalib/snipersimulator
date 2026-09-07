#!/usr/bin/env python2
"""
Collect + compute PPW from a REAL Sniper+McPAT run's output directory -- not a
surrogate estimate -- and, given multiple arms of the same benchmark, the three
headline metrics from FINAL_EXPERIMENT.md: savings_vs_no_change,
savings_vs_best_static, headroom_captured. Run on the HOST (python2, needs
sniper_lib/sniper_config from tools/, same environment tools/mcpat.py already
runs in -- see ReconfigurationManager::triggerPowerSample()).

PPW definition (matches training / tools/reconfig/estimate_ppw.py's surrogate
target -- pythonScripts/addCalculatedColumnsToMergedCsv.py:80-84):
    time_seconds = elapsed_time_fs / 1e15      (raw sniper_lib unit is
                                                 femtoseconds -- NOT the /1e9
                                                 FINAL_EXPERIMENT.md's own
                                                 formula uses, which refers to
                                                 an already-nanosecond training
                                                 CSV column, a different input)
    ips          = total_instructions / time_seconds
    PPW          = ips**3 / total_power        (total_power = McPAT Processor
                                                 "Runtime Dynamic", Watts --
                                                 dynamic-only, matching training;
                                                 a leakage-inclusive variant is
                                                 also computed and reported
                                                 separately, per the plan's
                                                 "Threats to validity")

Data sources, all real measurements, no model/surrogate involved:
  - sim.stats / sim.cfg (via sniper_lib.get_results, --partial-style windows)
    for per-interval and whole-run instruction counts and elapsed time.
  - power-<t0>-<t1>-<duration>.txt (McPAT text output, one per reconfig
    interval -- see ReconfigurationManager::triggerPowerSample()) for
    per-interval power.
  - sniper_reconfig_decisions.csv (if present) for how many intervals actually
    changed the live configuration (prev != new), not just how many the
    predictor was asked about.

Usage:
  Per-run summary:
    python2 analyze_final_experiment.py summarize --dir /root/results/barnes/dynamic_rf

  Cross-arm comparison for one benchmark:
    python2 analyze_final_experiment.py compare --benchmark barnes \
        --no-change /root/results/barnes/no_change \
        --best-static /root/results/barnes/best_static \
        --dynamic-rf /root/results/barnes/dynamic_rf \
        --max-resources /root/results/barnes/max_resources \
        --oracle-ppw <value>   # optional -- from static_vs_optimal.py's oracle
"""

import argparse
import csv
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))  # tools/ -- for sniper_lib/sniper_config

import sniper_lib


def parse_mcpat_txt(path):
    """Minimal standalone re-implementation of tools/mcpat.py main()'s component
    parser (~lines 143-191), WITHOUT re-running McPAT -- just reads an existing
    .txt output (the one triggerPowerSample() already produced) and returns
    {component_name: {stat_name: value}}. Unlike mcpat.py's own parser, this
    doesn't error on repeated component names (Core/L2/L3 appear once per core)
    since the only component this script actually reads is the single chip-wide
    "Processor" total."""
    with open(path) as f:
        text = f.read()
    components = text.split('*' * 89)[2:-1]
    power_dat = {}
    for component in components:
        lines = component.strip().split('\n')
        if not lines or not lines[0].strip():
            continue
        componentname = lines[0].strip().strip(':')
        values = {}
        prefix = []
        spaces = []
        for line in lines[1:]:
            if not line.strip():
                continue
            if '=' in line:
                m = re.match(r' *([^=]+)= *([-+0-9.e]+)(nan)?', line)
                if m:
                    name = ('/'.join(prefix + [m.group(1)])).strip()
                    value = 0.0 if m.groups()[-1] == 'nan' else float(m.group(2))
                    values[name] = value
            else:
                m = re.match(r'^( *)([^:(]*)', line)
                if m:
                    j = len(m.group(1))
                    while spaces and j <= spaces[-1]:
                        spaces = spaces[:-1]
                        prefix = prefix[:-1]
                    spaces.append(j)
                    prefix.append(m.group(2).strip())
        power_dat[componentname] = values
    return power_dat


def processor_power(power_dat, leakage_inclusive=False):
    """Watts. Matches tools/mcpat.py's power_stack(): dynamic-only is what
    training's PPW used; leakage-inclusive adds both leakage terms, reported
    as a labelled secondary per FINAL_EXPERIMENT.md."""
    proc = power_dat.get('Processor', {})
    p = proc.get('Runtime Dynamic', 0.0)
    if leakage_inclusive:
        p += proc.get('Subthreshold Leakage with power gating', proc.get('Subthreshold Leakage', 0.0))
        p += proc.get('Gate Leakage', 0.0)
    return p


def find_power_files(resultsdir):
    """[(t0, t1, duration_ns, path), ...] sorted chronologically.

    Filenames are "power-<t0>-<t1>-<duration>.txt" where t0 is either the
    literal marker "roi-begin" (first interval only) or a plain integer-ns
    string, and t1/duration are always plain integers -- so splitting on '-'
    and taking the last two tokens as (t1, duration), with everything before
    that rejoined as t0, is unambiguous even though "roi-begin" itself
    contains a hyphen (a naive single regex gets this wrong)."""
    out = []
    for path in glob.glob(os.path.join(resultsdir, 'power-*.txt')):
        stem = os.path.basename(path)[len('power-'):-len('.txt')]
        parts = stem.split('-')
        if len(parts) < 3:
            continue
        try:
            dur_ns = int(parts[-1])
        except ValueError:
            continue
        t1 = parts[-2]
        t0 = '-'.join(parts[:-2])
        out.append((t0, t1, dur_ns, path))
    out.sort(key=lambda item: -1 if item[0] == 'roi-begin' else int(item[0]))
    return out


def get_window_stats(resultsdir, t0, t1):
    """(total_instructions, elapsed_time_fs_of_core0) for one --partial-style
    window, via the same sniper_lib.get_results() mechanism tools/mcpat.py
    itself uses for its own --partial handling."""
    results = sniper_lib.get_results(resultsdir=resultsdir, partial=(t0, t1))
    r = results['results']
    instrs = sum(r.get('performance_model.instruction_count', [0]))
    elapsed = r.get('performance_model.elapsed_time', [0])
    elapsed_fs = elapsed[0] if elapsed else 0
    return instrs, elapsed_fs


def compute_ppw(instructions, elapsed_time_fs, power_w):
    if elapsed_time_fs <= 0 or power_w <= 0:
        return None, None
    time_s = elapsed_time_fs / 1e15  # fs -> s
    ips = instructions / time_s
    ppw = (ips ** 3) / power_w
    return ips, ppw


def count_actual_reconfigs(decision_log_path):
    """{'n_rows': int, 'n_moved': int} -- n_moved counts intervals where at
    least one *_prev/*_new pair actually differs, not just status=='applied':
    a no-op/static arm's predictor also reports 'applied' every interval while
    echoing the same values back unchanged (see noop_predict.py)."""
    n_moved = 0
    n_rows = 0
    with open(decision_log_path) as f:
        reader = csv.DictReader(f)
        # '_prev' appears mid-string for per-core columns (l2_bytes_prev_core0),
        # not just at the end (l3_bytes_prev) -- endswith('_prev') silently missed
        # every per-core column and only ever checked l3_bytes_prev.
        prev_cols = [c for c in (reader.fieldnames or []) if '_prev' in c]
        for row in reader:
            n_rows += 1
            if row.get('status') != 'applied':
                continue
            for pc in prev_cols:
                nc = pc.replace('_prev', '_new', 1)
                if nc in row and row.get(pc) != row.get(nc):
                    n_moved += 1
                    break
    return {'n_rows': n_rows, 'n_moved': n_moved}


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(x):
    return 'n/a' if x is None else '%.4g' % x


def summarize_run(resultsdir, label=None):
    label = label or resultsdir
    power_files = find_power_files(resultsdir)
    if not power_files:
        print('[%s] WARNING: no power-*.txt files found in %s' % (label, resultsdir))
        return None

    per_interval = []
    for t0, t1, dur_ns, path in power_files:
        if dur_ns <= 0:
            continue
        power_dat = parse_mcpat_txt(path)
        p_dyn = processor_power(power_dat, leakage_inclusive=False)
        p_leak = processor_power(power_dat, leakage_inclusive=True)
        instrs, elapsed_fs = get_window_stats(resultsdir, t0, t1)
        ips, ppw = compute_ppw(instrs, elapsed_fs, p_dyn)
        per_interval.append({
            't0': t0, 't1': t1, 'duration_ns': dur_ns,
            'instructions': instrs, 'elapsed_time_fs': elapsed_fs,
            'power_dynamic_w': p_dyn, 'power_leakage_inclusive_w': p_leak,
            'ips': ips, 'ppw': ppw,
        })

    if not per_interval:
        print('[%s] WARNING: no usable (non-zero-duration) intervals' % label)
        return None

    # Drop interval 0 from the aggregate (cold caches, model saw no deltas yet)
    # per FINAL_EXPERIMENT.md's "Threats to validity" -- Warm-up.
    agg = per_interval[1:] if len(per_interval) > 1 else per_interval

    whole_instrs, whole_elapsed_fs = get_window_stats(resultsdir, 'roi-begin', 'roi-end')
    whole_time_s = whole_elapsed_fs / 1e15 if whole_elapsed_fs else 0.0
    total_dur = sum(r['duration_ns'] for r in agg)
    power_run_dyn = (sum(r['power_dynamic_w'] * r['duration_ns'] for r in agg) / total_dur
                      if total_dur else 0.0)
    power_run_leak = (sum(r['power_leakage_inclusive_w'] * r['duration_ns'] for r in agg) / total_dur
                       if total_dur else 0.0)
    ips_run, ppw_run = compute_ppw(whole_instrs, whole_elapsed_fs, power_run_dyn)
    energy_dyn_j = sum(r['power_dynamic_w'] * (r['duration_ns'] * 1e-9) for r in agg)
    energy_leak_j = sum(r['power_leakage_inclusive_w'] * (r['duration_ns'] * 1e-9) for r in agg)

    decision_log = os.path.join(resultsdir, 'sniper_reconfig_decisions.csv')
    n_reconfigs = count_actual_reconfigs(decision_log) if os.path.exists(decision_log) else None

    summary = {
        'label': label, 'resultsdir': resultsdir,
        'n_intervals': len(per_interval),
        'whole_run_instructions': whole_instrs,
        'whole_run_time_s': whole_time_s,
        'whole_run_ips': ips_run,
        'whole_run_power_dynamic_w': power_run_dyn,
        'whole_run_power_leakage_inclusive_w': power_run_leak,
        'whole_run_energy_dynamic_j': energy_dyn_j,
        'whole_run_energy_leakage_inclusive_j': energy_leak_j,
        'ppw_run': ppw_run,
        'per_interval_ppw_mean': _mean([r['ppw'] for r in agg]),
        'n_reconfigs': n_reconfigs,
        'per_interval': per_interval,
    }
    _print_summary(summary)
    return summary


def _print_summary(s):
    print('=' * 70)
    print('%s  (%s)' % (s['label'], s['resultsdir']))
    print('=' * 70)
    print('  intervals (McPAT samples):        %d' % s['n_intervals'])
    print('  whole-run instructions:           %s' % _fmt(s['whole_run_instructions']))
    print('  whole-run time (s):               %.4f' % (s['whole_run_time_s'] or 0))
    print('  whole-run IPS:                    %s' % _fmt(s['whole_run_ips']))
    print('  whole-run power, dynamic (W):     %.4f' % (s['whole_run_power_dynamic_w'] or 0))
    print('  whole-run power, +leakage (W):    %.4f' % (s['whole_run_power_leakage_inclusive_w'] or 0))
    print('  whole-run energy, dynamic (J):    %.4f' % s['whole_run_energy_dynamic_j'])
    print('  whole-run energy, +leakage (J):   %.4f' % s['whole_run_energy_leakage_inclusive_j'])
    print('  PPW (whole-run, dynamic power):   %s' % _fmt(s['ppw_run']))
    print('  PPW (mean per-interval):          %s' % _fmt(s['per_interval_ppw_mean']))
    if s['n_reconfigs'] is not None:
        print('  reconfig decisions:               %d/%d intervals actually changed the config' %
              (s['n_reconfigs']['n_moved'], s['n_reconfigs']['n_rows']))
    print('')


def compare_arms(benchmark, arms, oracle_ppw=None):
    """arms: {'no_change': dir, 'best_static': dir, 'dynamic_rf': dir,
    ['max_resources': dir]}. Prints FINAL_EXPERIMENT.md's three headline metrics."""
    summaries = {}
    for name, d in arms.items():
        s = summarize_run(d, label='%s/%s' % (benchmark, name))
        if s is None:
            print('Cannot compute comparison: %s arm produced no usable summary' % name)
            return None
        summaries[name] = s

    ppw = dict((name, s['ppw_run']) for name, s in summaries.items())
    for required in ('no_change', 'best_static', 'dynamic_rf'):
        if ppw.get(required) is None:
            print('Need a valid PPW for no_change, best_static, and dynamic_rf to compare '
                  '(missing or zero: %s).' % required)
            return None

    savings_vs_no_change = (ppw['dynamic_rf'] - ppw['no_change']) / ppw['no_change'] * 100.0
    savings_vs_best_static = (ppw['dynamic_rf'] - ppw['best_static']) / ppw['best_static'] * 100.0

    headroom = None
    if oracle_ppw:
        denom = oracle_ppw - ppw['best_static']
        headroom = ((ppw['dynamic_rf'] - ppw['best_static']) / denom * 100.0) if denom else None

    print('=' * 70)
    print('HEADLINE COMPARISON -- %s' % benchmark)
    print('=' * 70)
    for name in ('no_change', 'best_static', 'dynamic_rf', 'max_resources'):
        if name in ppw:
            print('  PPW[%-14s] = %s' % (name, _fmt(ppw[name])))
    print('')
    print('  savings_vs_no_change   = %+.2f%%' % savings_vs_no_change)
    print('  savings_vs_best_static = %+.2f%%' % savings_vs_best_static)
    if headroom is not None:
        print('  headroom_captured      = %.2f%%   (oracle PPW = %s)' % (headroom, _fmt(oracle_ppw)))
    else:
        print("  headroom_captured      = n/a (pass --oracle-ppw; see static_vs_optimal.py's "
              'per-interval oracle for this benchmark)')
    if 'max_resources' in ppw:
        beats = ppw['max_resources'] > ppw['best_static']
        print('')
        print('  sanity check: max_resources %s best_static (%s vs %s)%s' % (
            '>' if beats else '<=', _fmt(ppw['max_resources']), _fmt(ppw['best_static']),
            '  <-- widen the finalist set, see FINAL_EXPERIMENT.md' if beats else ''))

    return {
        'benchmark': benchmark, 'ppw': ppw,
        'savings_vs_no_change': savings_vs_no_change,
        'savings_vs_best_static': savings_vs_best_static,
        'headroom_captured': headroom,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd')

    p1 = sub.add_parser('summarize', help='Per-run PPW/energy summary')
    p1.add_argument('--dir', required=True)
    p1.add_argument('--label', default=None)

    p2 = sub.add_parser('compare', help='Cross-arm headline comparison for one benchmark')
    p2.add_argument('--benchmark', required=True)
    p2.add_argument('--no-change', dest='no_change', required=True)
    p2.add_argument('--best-static', dest='best_static', required=True)
    p2.add_argument('--dynamic-rf', dest='dynamic_rf', required=True)
    p2.add_argument('--max-resources', dest='max_resources', default=None)
    p2.add_argument('--oracle-ppw', type=float, default=None)

    args = ap.parse_args()
    if args.cmd == 'summarize':
        summarize_run(args.dir, label=args.label)
    elif args.cmd == 'compare':
        arms = {'no_change': args.no_change, 'best_static': args.best_static,
                'dynamic_rf': args.dynamic_rf}
        if args.max_resources:
            arms['max_resources'] = args.max_resources
        compare_arms(args.benchmark, arms, oracle_ppw=args.oracle_ppw)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
