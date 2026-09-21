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


def summarize_run(resultsdir, label=None, show_per_interval=False, quiet=False):
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
    if not quiet:
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


def diagnose_decisions(benchmark, dynamic_rf_dir, no_change_dir, best_static_dir=None):
    """Judges each of dynamic_rf's actual reconfig decisions against a real counterfactual:
    no_change's per-interval PPW at that SAME interval index (interval boundaries are
    instruction-count-based -- see interval_performance_model.cc -- so interval i means
    roughly the same point in the program's execution across arms run on the same
    benchmark/instruction budget, making this an apples-to-apples "what if we'd just left
    it alone at this exact phase" comparison, not merely dynamic_rf's own before/after).
    best_static (optional) additionally shows how much headroom was left on the table.
    """
    s_rf = summarize_run(dynamic_rf_dir, label='%s/dynamic_rf' % benchmark, quiet=True)
    s_nc = summarize_run(no_change_dir, label='%s/no_change' % benchmark, quiet=True)
    if s_rf is None or s_nc is None:
        print('Cannot diagnose: dynamic_rf or no_change arm produced no usable summary')
        return None
    s_bs = summarize_run(best_static_dir, label='%s/best_static' % benchmark, quiet=True) \
        if best_static_dir else None

    decision_log = os.path.join(dynamic_rf_dir, 'sniper_reconfig_decisions.csv')
    if not os.path.exists(decision_log):
        print('No decision log at %s' % decision_log)
        return None
    with open(decision_log) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        decision_rows = list(reader)

    pi_rf, pi_nc = s_rf['per_interval'], s_nc['per_interval']
    pi_bs = s_bs['per_interval'] if s_bs else None
    n = min(len(pi_rf), len(pi_nc), len(decision_rows))
    rows = []  # one entry per actual TRANSITION (this interval's active config != previous
               # interval's), matching count_actual_reconfigs()/ppw_stable_vs_changed()'s own
               # convention -- NOT "differs from interval 0", which would also flag every
               # interval downstream of one lasting change and drown out the real signal.
    all_deltas_vs_nc = []   # dynamic_rf PPW - no_change PPW, every interval (not just transitions)
    all_gaps_vs_bs = []     # best_static PPW - dynamic_rf PPW, every interval

    for i in range(1, n):  # interval 0 excluded: cold caches, same convention as summarize_run
        cfg_i = _row_prev_config_tuple(decision_rows[i], fieldnames)
        cfg_prev = _row_prev_config_tuple(decision_rows[i - 1], fieldnames)
        ppw_rf_i, ppw_nc_i = pi_rf[i]['ppw'], pi_nc[i]['ppw']
        if ppw_rf_i is not None and ppw_nc_i is not None:
            all_deltas_vs_nc.append(ppw_rf_i - ppw_nc_i)
        if pi_bs is not None and i < len(pi_bs) and pi_bs[i]['ppw'] is not None and ppw_rf_i is not None:
            all_gaps_vs_bs.append(pi_bs[i]['ppw'] - ppw_rf_i)

        if cfg_i != cfg_prev:
            delta = (ppw_rf_i - ppw_nc_i) if (ppw_rf_i is not None and ppw_nc_i is not None) else None
            changed_dims = [pc.replace('_prev', '', 1) for pc, v0, vi in
                             zip(_prev_cols(fieldnames), cfg_prev, cfg_i) if v0 != vi]
            rows.append({
                'interval': decision_rows[i].get('interval', i), 'changed_dims': changed_dims,
                'cfg': dict(zip(_prev_cols(fieldnames), cfg_i)),
                'ppw_rf': ppw_rf_i, 'ppw_nc': ppw_nc_i, 'delta_vs_nochange': delta,
            })

    print('=' * 70)
    print('DECISION DIAGNOSIS -- %s' % benchmark)
    print('=' * 70)
    print('  %d actual transitions out of %d intervals (%d skipped as warm-up)' %
          (len(rows), n - 1, 1))
    print('')
    if rows:
        print('  %-8s %-30s %12s %12s %10s' % ('interval', 'changed dims', 'PPW(rf)', 'PPW(no_change)', 'delta'))
        for r in rows:
            print('  %-8s %-30s %12s %12s %+9.1f%%' % (
                r['interval'], ','.join(r['changed_dims']) or '(none? bug)',
                _fmt(r['ppw_rf']), _fmt(r['ppw_nc']),
                (r['delta_vs_nochange'] / r['ppw_nc'] * 100.0)
                if r['delta_vs_nochange'] is not None and r['ppw_nc'] else float('nan')))
        deltas = [r['delta_vs_nochange'] for r in rows if r['delta_vs_nochange'] is not None]
        n_helped = sum(1 for d in deltas if d > 0)
        n_hurt = sum(1 for d in deltas if d < 0)
        print('')
        print('  of %d judged decisions: %d helped (PPW > no_change at that phase), %d hurt' %
              (len(deltas), n_helped, n_hurt))
        print('  mean PPW delta on decision intervals: %s (vs no_change, same phase)' % _fmt(_mean(deltas)))
    print('')
    print('  mean PPW delta, ALL intervals (dynamic_rf - no_change, same phase): %s' %
          _fmt(_mean(all_deltas_vs_nc)))
    if all_gaps_vs_bs:
        print('  mean PPW gap,  ALL intervals (best_static - dynamic_rf, same phase): %s'
              '  <- average headroom left on the table' % _fmt(_mean(all_gaps_vs_bs)))
    return {'rows': rows, 'mean_delta_vs_nochange': _mean(all_deltas_vs_nc),
            'mean_gap_vs_best_static': _mean(all_gaps_vs_bs) if all_gaps_vs_bs else None}


def shrink_policy_ab(root):
    """Compare reconfig/shrink_policy=clamp vs flush, per benchmark, under
    results/<bench>/{clamp,flush}. Both arms are dynamic_rf with an identical starting
    config, so the only difference is whether a shrink that doesn't fit is refused
    (clamp) or made to fit by evicting first (flush)."""
    benches = sorted(d for d in os.listdir(root)
                     if os.path.isdir(os.path.join(root, d)) and not d.startswith('.'))
    print('=' * 86)
    print('SHRINK POLICY A/B  --  clamp (refuse shrink) vs flush (evict, then shrink)')
    print('=' * 86)
    print('  %-12s %10s %7s %7s %12s %12s %9s' % (
        'benchmark', 'policy', 'ivals', 'moves', 'PPW', 'IPS', 'power W'))
    rows = {}
    for bench in benches:
        for policy in ('clamp', 'flush'):
            d = os.path.join(root, bench, policy)
            if not os.path.isdir(d) or not os.path.exists(os.path.join(d, 'sim.out')):
                continue
            s = summarize_run(d, label='%s/%s' % (bench, policy), quiet=True)
            if not s or not s['ppw_run']:
                continue
            rows[(bench, policy)] = s
            moves = _run_transitions(d)
            print('  %-12s %10s %7d %7s %12s %12s %9.2f' % (
                bench, policy, s['n_intervals'], '-' if moves is None else moves,
                _fmt(s['ppw_run']), _fmt(s['whole_run_ips']),
                s['whole_run_power_dynamic_w']))
    print('')
    print('  %-12s %14s %14s   %s' % ('benchmark', 'PPW(clamp)', 'PPW(flush)', 'flush vs clamp'))
    ratios = []
    for bench in benches:
        c, f = rows.get((bench, 'clamp')), rows.get((bench, 'flush'))
        if not c or not f:
            continue
        r = f['ppw_run'] / c['ppw_run']
        ratios.append(r)
        print('  %-12s %14s %14s   %+.2f%%' % (
            bench, _fmt(c['ppw_run']), _fmt(f['ppw_run']), (r - 1) * 100.0))
    if ratios:
        import math
        gm = math.exp(sum(math.log(x) for x in ratios) / len(ratios))
        print('')
        print('  geomean flush / clamp = %.4fx  (%+.2f%%, n=%d)' % (gm, (gm - 1) * 100.0, len(ratios)))
        print('')
        print('  Reading it: flush only helps if the model\'s shrink requests were both')
        print('  correct AND being refused under clamp. A negative result means either the')
        print('  shrinks were wrong, or the eviction traffic cost more than the smaller')
        print('  cache saved -- check the flushed-line counts in status.csv to tell which.')
    return rows


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
    # Only dynamic_rf is mandatory. no_change/best_static are frequently absent by design:
    # best_static exists only for the 4 sweep benchmarks (and even there it is a 2-core
    # `-c rob` config), and no_change is just another small static point. Report whichever
    # baselines are actually present rather than refusing to compare.
    if ppw.get('dynamic_rf') is None:
        print('Need a valid PPW for dynamic_rf to compare.')
        return None

    def _delta(baseline):
        b = ppw.get(baseline)
        if b is None or not b:
            return None
        return (ppw['dynamic_rf'] - b) / b * 100.0

    savings_vs_no_change = _delta('no_change')
    savings_vs_best_static = _delta('best_static')
    savings_vs_max_resources = _delta('max_resources')
    savings_vs_max_resources_nopf = _delta('max_resources_nopf')

    headroom = None
    if oracle_ppw and ppw.get('best_static'):
        denom = oracle_ppw - ppw['best_static']
        headroom = ((ppw['dynamic_rf'] - ppw['best_static']) / denom * 100.0) if denom else None

    print('=' * 70)
    print('HEADLINE COMPARISON -- %s' % benchmark)
    print('=' * 70)
    for name in ('no_change', 'best_static', 'dynamic_rf', 'max_resources', 'max_resources_nopf'):
        if name in ppw:
            print('  PPW[%-18s] = %s' % (name, _fmt(ppw[name])))
    print('')
    for label, val in (('savings_vs_no_change', savings_vs_no_change),
                        ('savings_vs_best_static', savings_vs_best_static),
                        ('savings_vs_max_resources', savings_vs_max_resources),
                        ('savings_vs_max_resources_nopf', savings_vs_max_resources_nopf)):
        if val is not None:
            print('  %-30s = %+.2f%%' % (label, val))
    if headroom is not None:
        print('  %-30s = %.2f%%   (oracle PPW = %s)' % ('headroom_captured', headroom, _fmt(oracle_ppw)))

    # max_resources has collapsed (~4-5x slowdown) in every run so far, which makes a
    # percentage against it enormous but hard to attribute. If the no-prefetcher variant
    # is present, say which factor is responsible rather than leaving it ambiguous.
    if savings_vs_max_resources is not None and savings_vs_max_resources_nopf is not None:
        print('')
        if ppw['max_resources_nopf'] > ppw['max_resources'] * 2:
            print('  NOTE: max_resources_nopf is %.1fx max_resources -- the collapse is the'
                  % (ppw['max_resources_nopf'] / ppw['max_resources']))
            print('        PREFETCHER, not the cache sizes. Quote savings_vs_max_resources_nopf;')
            print('        savings_vs_max_resources mostly measures a misconfigured prefetcher.')
        else:
            print('  NOTE: disabling the prefetcher does not recover max_resources'
                  ' (%.2fx) -- the' % (ppw['max_resources_nopf'] / ppw['max_resources']))
            print('        collapse is attributable to over-provisioning itself, so'
                  ' savings_vs_max_resources stands.')

    return {
        'benchmark': benchmark, 'ppw': ppw,
        'savings_vs_no_change': savings_vs_no_change,
        'savings_vs_best_static': savings_vs_best_static,
        'savings_vs_max_resources': savings_vs_max_resources,
        'savings_vs_max_resources_nopf': savings_vs_max_resources_nopf,
        'headroom_captured': headroom,
    }


def _input_class_map(resultsroot):
    """{(benchmark, arm): input_class} from the sweep's own status CSV.

    NOT from sim.info: that records the *sniper* command line, and run-sniper consumes
    --benchmarks itself (handing sniper a trace), so the SPLASH input class never appears
    there. run_n4_sweep.sh writes it per (benchmark, arm) instead."""
    out = {}
    path = os.path.join(resultsroot, 'sweep_status.csv')
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                if row.get('input'):
                    out[(row.get('benchmark'), row.get('arm'))] = row['input']
    except Exception:
        pass
    return out


def _run_transitions(resultsdir):
    """Number of intervals whose ACTIVE config differs from the previous interval's."""
    path = os.path.join(resultsdir, 'sniper_reconfig_decisions.csv')
    if not os.path.exists(path):
        return None
    with open(path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    prev_cols = _prev_cols(fieldnames)
    n, last = 0, None
    for r in rows:
        cur = tuple(r.get(c) for c in prev_cols)
        if last is not None and cur != last:
            n += 1
        last = cur
    return n


def sweep_summary(resultsroot, arms=None):
    """Tabulate every benchmark under resultsroot/<benchmark>/<arm>/, reporting
    dynamic_rf's PPW gain over each baseline present. Benchmarks whose arms are missing
    or crashed (no sim.out) are listed as skipped rather than silently dropped."""
    arms = arms or ['dynamic_rf', 'max_resources', 'max_resources_nopf', 'best_static', 'no_change']
    benches = sorted(d for d in os.listdir(resultsroot)
                     if os.path.isdir(os.path.join(resultsroot, d)))
    rows, skipped = [], []
    for bench in benches:
        ppw = {}
        for arm in arms:
            d = os.path.join(resultsroot, bench, arm)
            if not os.path.isdir(d):
                continue
            if not os.path.exists(os.path.join(d, 'sim.out')):
                skipped.append('%s/%s (crashed: no sim.out)' % (bench, arm))
                continue
            s = summarize_run(d, label='%s/%s' % (bench, arm), quiet=True)
            if s and s['ppw_run']:
                ppw[arm] = s['ppw_run']
        if 'dynamic_rf' not in ppw:
            skipped.append('%s (no usable dynamic_rf)' % bench)
            continue
        rows.append((bench, ppw))

    print('=' * 78)
    print('N=4 SWEEP SUMMARY -- dynamic_rf PPW gain over each baseline')
    print('=' * 78)
    inputs = _input_class_map(resultsroot)
    print('  %-12s %-7s %6s %6s %11s %11s %11s %11s' % (
        'benchmark', 'input', 'ivals', 'moves', 'dynamic_rf', 'vs max_res', 'vs max_nopf', 'vs best_stat'))
    def pct(d, b):
        return '%+.1f%%' % ((d - b) / b * 100.0) if b else 'n/a'
    for bench, ppw in rows:
        d = ppw['dynamic_rf']
        ddir = os.path.join(resultsroot, bench, 'dynamic_rf')
        inp = inputs.get((bench, 'dynamic_rf'), '?')
        moves = _run_transitions(ddir)
        n_iv = len(glob.glob(os.path.join(ddir, 'power-*.txt')))
        print('  %-12s %-7s %6d %6s %11s %11s %11s %11s' % (
            bench, inp, n_iv, ('-' if moves is None else moves), _fmt(d),
            pct(d, ppw['max_resources']) if 'max_resources' in ppw else 'n/a',
            pct(d, ppw['max_resources_nopf']) if 'max_resources_nopf' in ppw else 'n/a',
            pct(d, ppw['best_static']) if 'best_static' in ppw else 'n/a'))
    if rows:
        # Geometric mean of the ratio is the right average for a ratio-of-ratios metric;
        # an arithmetic mean of percentages would be skewed by max_resources' collapse.
        import math
        for base in ('max_resources', 'max_resources_nopf'):
            ratios = [ppw['dynamic_rf'] / ppw[base] for _, ppw in rows if ppw.get(base)]
            if ratios:
                gm = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
                print('')
                print('  geomean dynamic_rf / %-18s = %.2fx  (%+.1f%%, n=%d)'
                      % (base, gm, (gm - 1) * 100.0, len(ratios)))
    if skipped:
        print('')
        print('  skipped:')
        for s in skipped:
            print('    - %s' % s)
    return rows


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
    p2.add_argument('--no-change', dest='no_change', default=None)
    p2.add_argument('--best-static', dest='best_static', default=None)
    p2.add_argument('--dynamic-rf', dest='dynamic_rf', required=True)
    p2.add_argument('--max-resources', dest='max_resources', default=None)
    p2.add_argument('--max-resources-nopf', dest='max_resources_nopf', default=None)
    p2.add_argument('--oracle-ppw', type=float, default=None)

    p3 = sub.add_parser('diagnose',
                         help="Judge dynamic_rf's individual reconfig decisions against "
                              'no_change at the same interval (real counterfactual, not '
                              "just dynamic_rf's own before/after)")
    p3.add_argument('--benchmark', required=True)
    p3.add_argument('--dynamic-rf', dest='dynamic_rf', required=True)
    p3.add_argument('--no-change', dest='no_change', required=True)
    p3.add_argument('--best-static', dest='best_static', default=None,
                     help='Optional: also report average headroom left vs best_static')

    p5 = sub.add_parser('shrink-ab', help='Compare shrink_policy clamp vs flush')
    p5.add_argument('--root', required=True)

    p4 = sub.add_parser('sweep', help='Tabulate every benchmark under a results root '
                                       '(e.g. results/n4_hetero) vs each baseline')
    p4.add_argument('--root', required=True)

    args = ap.parse_args()
    if args.cmd == 'summarize':
        summarize_run(args.dir, label=args.label, show_per_interval=args.per_interval)
    elif args.cmd == 'compare':
        arms = {'dynamic_rf': args.dynamic_rf}
        for name, d in (('no_change', args.no_change), ('best_static', args.best_static),
                        ('max_resources', args.max_resources),
                        ('max_resources_nopf', args.max_resources_nopf)):
            if d:
                arms[name] = d
        compare_arms(args.benchmark, arms, oracle_ppw=args.oracle_ppw)
    elif args.cmd == 'diagnose':
        diagnose_decisions(args.benchmark, args.dynamic_rf, args.no_change, args.best_static)
    elif args.cmd == 'sweep':
        sweep_summary(args.root)
    elif args.cmd == 'shrink-ab':
        shrink_policy_ab(args.root)
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
