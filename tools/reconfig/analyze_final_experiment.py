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


def load_decision_rows(decision_log_path):
    with open(decision_log_path) as f:
        return list(csv.DictReader(f))


def _prev_cols(fieldnames):
    """'_prev' appears mid-string for per-core columns (l2_bytes_prev_core0),
    not just at the end (l3_bytes_prev)."""
    return [c for c in (fieldnames or []) if '_prev' in c]


def count_actual_reconfigs(decision_rows, fieldnames):
    """{'n_rows': int, 'n_moved': int} -- n_moved counts intervals where at
    least one *_prev/*_new pair actually differs, not just status=='applied':
    a no-op/static arm's predictor also reports 'applied' every interval while
    echoing the same values back unchanged (see noop_predict.py)."""
    n_moved = 0
    prev_cols = _prev_cols(fieldnames)
    for row in decision_rows:
        if row.get('status') != 'applied':
            continue
        for pc in prev_cols:
            nc = pc.replace('_prev', '_new', 1)
            if nc in row and row.get(pc) != row.get(nc):
                n_moved += 1
                break
    return {'n_rows': len(decision_rows), 'n_moved': n_moved}


def per_dimension_changes(decision_rows, fieldnames):
    """{dimension_column: n_changed} -- how many 'applied' intervals actually
    moved EACH dimension (l2_bytes_core0, btb_entries_core0, prefetch_core0,
    l3_bytes, ...) individually, not just "did anything change" as a whole.
    Column names here are the _prev column with that suffix stripped, e.g.
    'l2_bytes_prev_core0' -> 'l2_bytes_core0'."""
    counts = {}
    for pc in _prev_cols(fieldnames):
        dim = pc.replace('_prev', '', 1)
        nc = pc.replace('_prev', '_new', 1)
        n = 0
        for row in decision_rows:
            if row.get('status') != 'applied':
                continue
            if nc in row and row.get(pc) != row.get(nc):
                n += 1
        counts[dim] = n
    return counts


def _row_prev_config_tuple(row, fieldnames):
    """This row's _prev values as a tuple, in a fixed column order -- what was
    ACTUALLY ACTIVE during the interval this row corresponds to (dumpIntervalStats()
    re-queries live cache/BTB/prefetcher state for "_prev" at the start of handling
    each interval, so row[i]'s _prev IS the config per_interval[i]'s power/PPW was
    measured under -- not row[i]'s own _new, which only takes effect starting
    next interval)."""
    return tuple(row.get(c) for c in _prev_cols(fieldnames))


def config_popularity(decision_rows, fieldnames, top_n=5):
    """[(config_tuple, count), ...] -- the most common ACTIVE (prev) configs
    across all intervals, most-common first. Uses _prev (not _new) for the same
    reason as _row_prev_config_tuple: it's what was actually in effect."""
    prev_cols = _prev_cols(fieldnames)
    counter = {}
    for row in decision_rows:
        key = _row_prev_config_tuple(row, fieldnames)
        counter[key] = counter.get(key, 0) + 1
    ranked = sorted(counter.items(), key=lambda kv: -kv[1])[:top_n]
    return prev_cols, ranked


def ppw_stable_vs_changed(per_interval, decision_rows, fieldnames):
    """Mean/median PPW for intervals whose ACTIVE config just changed from the
    previous interval's (row[i]._prev != row[i-1]._prev) vs intervals where it
    held steady. Answers "does PPW dip/improve right after a reconfiguration,
    e.g. from the fixed transition penalty or a genuinely better/worse choice."
    Positional pairing: per_interval[i] and decision_rows[i] share the same
    m_interval_index (both written once per handleReconfiguration() call)."""
    n = min(len(per_interval), len(decision_rows))
    changed_ppw, stable_ppw = [], []
    for i in range(1, n):
        cur = _row_prev_config_tuple(decision_rows[i], fieldnames)
        prv = _row_prev_config_tuple(decision_rows[i - 1], fieldnames)
        bucket = changed_ppw if cur != prv else stable_ppw
        if per_interval[i]['ppw'] is not None:
            bucket.append(per_interval[i]['ppw'])
    return {
        'changed_mean': _mean(changed_ppw), 'changed_n': len(changed_ppw),
        'stable_mean': _mean(stable_ppw), 'stable_n': len(stable_ppw),
    }


def _percentile(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _fmt(x):
    return 'n/a' if x is None else '%.4g' % x


def summarize_run(resultsdir, label=None, show_per_interval=False):
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
    decision_rows, fieldnames = [], []
    n_reconfigs = per_dim = pop_cols = pop_ranked = stable_vs_changed = None
    if os.path.exists(decision_log):
        with open(decision_log) as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            decision_rows = list(reader)
        n_reconfigs = count_actual_reconfigs(decision_rows, fieldnames)
        per_dim = per_dimension_changes(decision_rows, fieldnames)
        pop_cols, pop_ranked = config_popularity(decision_rows, fieldnames)
        stable_vs_changed = ppw_stable_vs_changed(per_interval, decision_rows, fieldnames)

    ppw_values = [r['ppw'] for r in agg]
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
        'per_interval_ppw_mean': _mean(ppw_values),
        'per_interval_ppw_median': _percentile(ppw_values, 0.5),
        'per_interval_ppw_p10': _percentile(ppw_values, 0.10),
        'per_interval_ppw_p90': _percentile(ppw_values, 0.90),
        'per_interval_ppw_min': (min(x for x in ppw_values if x is not None)
                                   if any(x is not None for x in ppw_values) else None),
        'per_interval_ppw_max': (max(x for x in ppw_values if x is not None)
                                   if any(x is not None for x in ppw_values) else None),
        'n_reconfigs': n_reconfigs,
        'per_dimension_changes': per_dim,
        'config_popularity_cols': pop_cols,
        'config_popularity': pop_ranked,
        'ppw_stable_vs_changed': stable_vs_changed,
        'per_interval': per_interval,
    }
    _print_summary(summary, show_per_interval=show_per_interval)
    return summary


def _print_summary(s, show_per_interval=False):
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
    print('  PPW per-interval: mean=%s median=%s p10=%s p90=%s min=%s max=%s' % (
        _fmt(s['per_interval_ppw_mean']), _fmt(s['per_interval_ppw_median']),
        _fmt(s['per_interval_ppw_p10']), _fmt(s['per_interval_ppw_p90']),
        _fmt(s['per_interval_ppw_min']), _fmt(s['per_interval_ppw_max'])))

    if s['n_reconfigs'] is not None:
        print('  reconfig decisions:               %d/%d intervals actually changed the config' %
              (s['n_reconfigs']['n_moved'], s['n_reconfigs']['n_rows']))

    if s['per_dimension_changes']:
        print('')
        print('  Per-dimension change counts (out of %d intervals):' % s['n_reconfigs']['n_rows'])
        for dim, n in sorted(s['per_dimension_changes'].items()):
            print('    %-22s %d' % (dim, n))

    if s['config_popularity']:
        print('')
        print('  Most common ACTIVE configs (what was really in effect, by interval count):')
        cols = s['config_popularity_cols']
        for cfg_tuple, count in s['config_popularity']:
            pct = 100.0 * count / s['n_reconfigs']['n_rows'] if s['n_reconfigs']['n_rows'] else 0.0
            desc = ', '.join('%s=%s' % (c.replace('_prev', ''), v) for c, v in zip(cols, cfg_tuple))
            print('    %3d (%5.1f%%)  %s' % (count, pct, desc))

    svc = s['ppw_stable_vs_changed']
    if svc and (svc['changed_n'] or svc['stable_n']):
        print('')
        print('  PPW: right after a config change vs. holding steady from the previous interval:')
        print('    just changed (n=%-4d): mean PPW = %s' % (svc['changed_n'], _fmt(svc['changed_mean'])))
        print('    held steady  (n=%-4d): mean PPW = %s' % (svc['stable_n'], _fmt(svc['stable_mean'])))
        if svc['changed_mean'] is not None and svc['stable_mean'] not in (None, 0):
            delta_pct = (svc['changed_mean'] - svc['stable_mean']) / svc['stable_mean'] * 100.0
            print('    delta: %+.2f%% (transition penalty / adaptation cost shows up here if negative)' % delta_pct)

    if show_per_interval:
        print('')
        print('  Per-interval detail:')
        print('    %6s %14s %14s %10s %10s %14s' % ('idx', 't0', 't1', 'instrs', 'power(W)', 'ppw'))
        for i, r in enumerate(s['per_interval']):
            print('    %6d %14s %14s %10s %10.4f %14s' % (
                i, r['t0'], r['t1'], _fmt(r['instructions']), r['power_dynamic_w'] or 0, _fmt(r['ppw'])))

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
    p1.add_argument('--per-interval', action='store_true', default=False,
                     help='Also print a row per interval (t0/t1/instructions/power/PPW).')

    p2 = sub.add_parser('compare', help='Cross-arm headline comparison for one benchmark')
    p2.add_argument('--benchmark', required=True)
    p2.add_argument('--no-change', dest='no_change', required=True)
    p2.add_argument('--best-static', dest='best_static', required=True)
    p2.add_argument('--dynamic-rf', dest='dynamic_rf', required=True)
    p2.add_argument('--max-resources', dest='max_resources', default=None)
    p2.add_argument('--oracle-ppw', type=float, default=None)

    args = ap.parse_args()
    if args.cmd == 'summarize':
        summarize_run(args.dir, label=args.label, show_per_interval=args.per_interval)
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
