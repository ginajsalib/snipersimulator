import os
import re
import csv
import parse_mcpat_power  # assumes parse_mcpat_power.py is in the same dir

def collect_directories(base_dirs, benchmark='barnes', only=None):
    """
    base_dirs: a single path string, or a list of path strings to scan.
    """
    if isinstance(base_dirs, str):
        base_dirs = [base_dirs]

    dirs = []
    # benchmark was hardcoded to 'barnes' here, which silently returned zero
    # directories for every other benchmark.
    pattern = re.compile(
    r'config_l2_(\d+)(?:_(\d+))?_l3MB_(\d+)_prefetch_(none|simple)(?:[-_](none|simple))?_branch_(\d+)-(\d+)_'
    + re.escape(benchmark) + r'-intervals$')
    for base_dir in base_dirs:
        if not os.path.isdir(base_dir):
            print("[Warning] base dir does not exist, skipping:", base_dir)
            continue
        for entry in os.listdir(base_dir):
            path = os.path.join(base_dir, entry)
            if os.path.isdir(path):
                if only is not None and entry not in only:
                    continue
                match = pattern.match(entry)
                if match:
                    l2_core0      = int(match.group(1))
                    l2_core1      = int(match.group(2)) if match.group(2) is not None else l2_core0
                    l3_size       = int(match.group(3))
                    prefetch_core0 = match.group(4)
                    prefetch_core1 = match.group(5) if match.group(5) is not None else prefetch_core0
                    btb0          = int(match.group(6))
                    btb1          = int(match.group(7))
                    dirs.append((path, l2_core0, l2_core1, l3_size, prefetch_core0, prefetch_core1, btb0, btb1))
                else:
                    # helpful for debugging: flag dirs that look relevant but didn't match
                    if 'config_' in entry and benchmark in entry.lower():
                        print("[Notice] dir looks relevant but did not match pattern:", path)
    return dirs

def collect_all_power(base_dirs, output_csv, benchmark_name, only=None):
    dirs = collect_directories(base_dirs, benchmark_name, only)
    results = []
    for path, l2_core0, l2_core1, l3_size, prefetch_core0, prefetch_core1, btb0, btb1 in dirs:
        files = [f for f in os.listdir(path) if f.startswith('power-') and f.endswith('.txt')]
        for file in files:
            filepath = os.path.join(path, file)
            data = parse_mcpat_power.parse_power_file(filepath)
            data['benchmark']      = benchmark_name
            data['l2Core0']        = l2_core0
            data['l2Core1']        = l2_core1
            data['l3Size']         = l3_size
            data['prefetchCore0']  = prefetch_core0
            data['prefetchCore1']  = prefetch_core1
            data['btbCore0']       = btb0
            data['btbCore1']       = btb1
            data['directory']      = path
            results.append(data)
    with open(output_csv, 'w', newline="", encoding="utf-8") as csvfile:
        fieldnames = [
    # identity
    'directory', 'benchmark',
    'l2Core0', 'l2Core1', 'l3Size',
    'prefetchCore0', 'prefetchCore1',
    'btbCore0', 'btbCore1',
    'file', 'period_start', 'period_end',
    # processor totals
    'total_runtime_dynamic', 'total_leakage', 'total_peak_dynamic',
    # per-core totals
    'core0_runtime_dynamic', 'core0_subthreshold_leakage', 'core0_gate_leakage',
    'core1_runtime_dynamic', 'core1_subthreshold_leakage', 'core1_gate_leakage',
    # L2 per core
    'l2_core0_runtime_dynamic', 'l2_core0_subthreshold_leakage', 'l2_core0_peak_dynamic',
    'l2_core1_runtime_dynamic', 'l2_core1_subthreshold_leakage', 'l2_core1_peak_dynamic',
    # BTB per core
    'btb_core0_runtime_dynamic', 'btb_core0_subthreshold_leakage',
    'btb_core1_runtime_dynamic', 'btb_core1_subthreshold_leakage',
    # branch predictor per core
    'branch_predictor_core0_runtime_dynamic',
    'branch_predictor_core1_runtime_dynamic',
    # IFU, EU, LSU per core
    'ifu_core0_runtime_dynamic',           'ifu_core1_runtime_dynamic',
    'execution_unit_core0_runtime_dynamic','execution_unit_core1_runtime_dynamic',
    'load_store_unit_core0_runtime_dynamic','load_store_unit_core1_runtime_dynamic',
    # L3
    'l3_runtime_dynamic', 'l3_subthreshold_leakage', 'l3_peak_dynamic',
    ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    print("Done. Parsed {} power files from {} configurations.".format(len(results), len(dirs)))
    print("Output saved to {}".format(output_csv))

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("Usage: python collect_all_power.py <base_dir1>[,base_dir2,...] <output_csv> <benchmark_name>")
    else:
        base_dirs_arg = sys.argv[1].split(',')
        # optional 4th arg: file listing directory basenames to restrict to
        only = None
        if len(sys.argv) > 4 and os.path.exists(sys.argv[4]):
            only = set(l.strip() for l in open(sys.argv[4]) if l.strip())
            print("[filter] restricting to %d directories" % len(only))
        collect_all_power(base_dirs_arg, sys.argv[2], sys.argv[3], only)
