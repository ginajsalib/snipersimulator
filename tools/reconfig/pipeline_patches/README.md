# Copies of the modified snipersim_framework pipeline scripts

These live outside this repo (in `~/Desktop/snipersim_framework`), so copies are kept
here for provenance. `.bak` files sit next to each original.

**objective_metric.py** (new) -- selectable objective for the selection stages:

| metric | expression | direction |
|---|---|---|
| `ppw` | ips^3 / power | higher better (original default, = 1/ED^2P) |
| `ppw_new` | ips / power | higher better (conventional perf-per-watt) |
| `ips` | ips | higher better (power ignored) |
| `power` | power | **lower better** (performance ignored) |

`add_objective()` emits a column where higher is always better, negating for `power`, so
`idxmax()` / `sort(ascending=False)` and the best-minus-other diffs work unchanged for
every metric. Reported values stay in natural units (watts, not negative watts).

**findBestConfigUsingPPW.py / findTop3ConfigsByPPW.py** -- take `--metric`; the output
columns keep their `PPW*` names so downstream stages are unaffected, with a new `metric`
column recording which objective produced them.

**run_pipeline.py** -- takes `--metric`; non-default metrics suffix the Stage 2/3 outputs
(`best_configs_<bench>_<metric>.csv` etc.) so runs under different objectives coexist.

**processIntervals.py** -- `benchmark_name` was hardcoded to `'barnes'` with no arguments.
Now `processIntervals.py [benchmark] [output_csv] [only_dirs_file]`; no arguments
reproduces the original behaviour. Must run INSIDE the container (it shells out to
`/root/sniper/tools/dumpstats.py` and walks `/export`).

**collect_all_power.py** -- its directory regex ended in `_barnes-intervals`, so it
silently returned zero directories for every other benchmark. Now follows the benchmark
argument it already accepted, plus an optional 4th argument restricting the walk.
