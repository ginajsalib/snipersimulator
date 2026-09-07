# Final experiment: live reconfiguration vs. static configuration, in PPW

## Claim to test

Running an N-core Sniper workload with `reconfig/enabled=true` (RF model driving
L2/L3 ways, BTB entries, prefetcher per core, McPAT re-sampled every interval)
yields higher **PPW** than holding any single fixed configuration for the whole
run.

## PPW definition — use the project's, not a textbook one

Every training CSV, the RF model's objective, and all the existing analysis
scripts use (`pythonScripts/addCalculatedColumnsToMergedCsv.py:80-84`):

```
time_seconds = performance_model.elapsed_time_core0 / 1e9
ips          = (Σ_c core.instructions_coreC) / time_seconds
PPW          = ips**3 / total_power
```

- `total_power` = McPAT **Processor "Runtime Dynamic"** in W, from a `--partial`
  interval sample (`parsePowerFiles.py:46-47`). Leakage is *not* in the training
  metric. The reconfig McPAT hook and `parse_mcpat_power.py` also expose
  `Total Leakage` / `Peak Dynamic` — collect them, but compute the headline PPW
  the same way training did (dynamic only) or every number is incomparable to the
  model's own evaluation. Report a leakage-inclusive variant alongside, labelled.
- `ips**3` (not `ips`) makes this a strongly performance-weighting efficiency
  metric — closer to `perf² / energy` than to instructions/joule. Racing-to-idle
  scores well under it. That is the thesis metric; keep it, but also report raw
  `runtime`, `ips`, average power, and energy so the result can be read both ways.

Two aggregation levels, report both:

1. **Per-interval** — one PPW per reconfig interval per arm (exactly what
   `dumpIntervalStats()` + the paired `power-*.txt` already give). Feeds the
   per-interval comparison and plots below.
2. **Whole-run** — `ips_run = total_instructions / total_runtime_s`,
   `power_run = Σ_i P_i·dur_i / Σ_i dur_i`, `PPW_run = ips_run**3 / power_run`.
   The single headline number per (benchmark, arm).

Use interval **deltas** for the per-interval ips (instructions and elapsed time
between successive stat dumps), matching how `dumpIntervalStats()` already
computes IPC — not cumulative counters.

## Phasing — status as of the 20260907_002136 training run (DONE, not projected)

- **Phase 1 — now, N=2.** Uses the already-trained, already-validated `rf_7way`
  model (`tools/reconfig/model/rf_7way_config_predictor_20260730_114244*`) via
  `rf_predict.py`. This is the *headline* experiment — the model is validated at
  exactly 2 cores, so the strongest claim lives here anyway. Nothing below is
  blocked on new training. `per_interval_oracle` in Phase 1 comes from the
  existing 2-core static sweep panel (`static_vs_optimal.py` /
  `merged_full_<bench>.csv`), not the surrogate.
- **Phase 2 — training has completed.** Bundle
  `tools/reconfig/model/ncore_20260907_002136_*` (per-core model, shared-L3
  model, surrogate, all scalers/imputers/encoders — copied in from
  `snipersim_framework/pythonScripts/saved_models/`, the big `.pkl`s stay
  gitignored per the existing convention). `rf_predict_ncore.py` will pick it up
  automatically (it globs for the newest `ncore_*_percore_model.pkl`). Real
  results from `ncore_20260907_002136_metadata.txt`, chunked training,
  benchmark-split, 4 SPLASH benchmarks:

  | metric | value |
  |---|---|
  | Per-core exact-match (l2+btb+prefetch all correct) | 4.58% |
  | L3 accuracy | 41.8% |
  | Composed exact-match (full config vs. real ground truth) | 0.09% |
  | Composed top-3 | 0.25% |
  | **PPW surrogate Pearson r (2-core-only)** | **0.737** |
  | Surrogate trustworthy (plan's own ≥0.8 gate) | **False** |

  **This changes what Phase 2 can claim.** The surrogate — the thing this plan
  designates as the N>2 oracle/pre-screen source — does not clear its own
  trustworthiness bar even restricted to the 2-core rows it was evaluated on.
  Concretely:
  - Do **not** use `estimate_ppw.py` as a `per_interval_oracle` or pre-screening
    source for any N>2 arm or headline number. Its output for N>2 configs is
    now *doubly* unvalidated: (a) the plan's own stated gate for extrapolating
    past 2 cores at all, which was never going to be met without 3-core ground
    truth, and (b) it no longer even clears 2-core trustworthiness in the first
    place.
  - The per-core/shared-L3 `percore_model`/`l3_model` predictions themselves
    (not the surrogate) are still usable for a `dynamic_rf` arm at N>2 — that's
    a real simulated + real McPAT-measured PPW, not a surrogate estimate. Phase
    2's `dynamic_rf` numbers stand; its `per_interval_oracle`/pre-screening
    numbers do not, until the surrogate improves or the 3-core validation set
    exists.
  - Phase 2 is still an *extension* section, labelled as beyond the validated
    regime, exactly as originally planned — just for a stronger reason now than
    "no 3-core ground truth yet": the fallback oracle itself is unreliable too.

Write the harness (`run_final_experiment.py` / `analyze_final_experiment.py`)
now against Phase 1; it parameterizes on core count and predictor script, so
Phase 2 is a config change, not a rewrite.

## Arms

| arm | how | baseline question it answers |
|---|---|---|
| `no_change` | `reconfig/enabled=true`, predictor = `noop_predict.py` (echoes current config) — config never moves off `base.cfg`'s starting point | "what if we never adapted?" |
| `best_static` | best single fixed config by mean PPW, chosen with hindsight from the static sweep for that benchmark; held constant all run | strongest non-adaptive competitor — the real bar |
| `dynamic_rf` | `reconfig/enabled=true`, predictor = `rf_predict.py` (Phase 1, N=2) or `rf_predict_ncore.py` (Phase 2) via the host bridge | the proposed system |
| `per_interval_oracle` | per-interval best config, surrogate-scored only (no sim) — `estimate_ppw.py` / the static sweep panel | upper bound; used for "headroom captured" |

These mirror `pythonScripts/ppw_savings_summary.py` exactly (`no-change`,
`best-static`, `headroom captured`). Keeping the same arm names means that
script's summary logic applies to the live results with only a data-loader swap.

## Headline metrics (same three as `ppw_savings_summary.py`)

```
savings_vs_no_change  = (PPW_dynamic - PPW_no_change)  / PPW_no_change  * 100
savings_vs_best_static = (PPW_dynamic - PPW_best_static) / PPW_best_static * 100
headroom_captured      = (PPW_dynamic - PPW_best_static) / (PPW_oracle - PPW_best_static) * 100
```

`headroom_captured` is the most defensible number for a thesis — baseline-
independent, bounded 0–100% (0 = no better than the best fixed config, 100 =
matches the per-interval oracle). Report it per benchmark and geomean across
{barnes, cholesky, fft, radiosity}.

## Workloads & static design space

SPLASH-2: **barnes, cholesky, fft, radiosity** (the four the model and surrogate
were trained on). Watch `fft` — it had all-NaN PPW in one earlier data cut
(`ppw_savings_summary.py:403`); verify its `total_power` parses before trusting
its numbers.

Static grid the RF model can emit (from
`plans/fluffy-tickling-shamir.md`): L2/core ∈ {256, 512, 1024} KB, L3 ∈ {4096,
8192, 16384} KB, BTB/core 4 values, prefetcher/core ∈ {none, simple, branch}.

Do **not** full-sweep this per benchmark. Pre-screen with the static sweep CSVs
(`pythonScripts/merged_full/merged_full_<bench>.csv` + `static_vs_optimal.py`)
— **not** the offline surrogate (`tools/reconfig/estimate_ppw.py`): the trained
surrogate's Pearson r is 0.737, below its own 0.8 trustworthiness gate (see
Phasing above), so it is not a reliable pre-screener even at 2 cores, let alone
for any N>2 finalist selection. The static sweep CSVs are real simulated PPW,
not an estimate, so they remain the right source regardless. Take the top ~3
static configs per benchmark ∪ the top ~3 by cross-benchmark mean, and run only
those (~8–12) as full static arms:

- best per benchmark → `best_static` for that benchmark
- best by cross-benchmark mean → a second "one fixed silicon design point" arm

`pythonScripts/static_vs_optimal.py` already computes best/median/worst static
PPW loss vs the per-interval oracle on a balanced panel — run it first to know
the offline-expected headroom before spending any simulation.

## Pipeline

```
tools/reconfig/run_final_experiment.py
  for bench in [barnes, cholesky, fft, radiosity]:
    for arm in [no_change, best_static, dynamic_rf, *static_grid_finalists]:
      cfg = materialize(base.cfg, arm, bench)      # reconfig/*, and fixed cache/BTB/pf for static arms
      run-sniper -c cfg -d results/<bench>/<arm> -s stop-by-icount:<N> -- <bench cmd>
      # dynamic_rf: start .reconfig_bridge_watch.py on the host first
      collect(results/<bench>/<arm>) -> results/summary.csv

collect(dir):
  per-interval: from sim.stats deltas  -> (instr_i, dt_i, ips_i)
                from power-<t0>-<t1>-*.txt -> P_i (Processor Runtime Dynamic), leakage_i
                ppw_i = ips_i**3 / P_i
  whole-run:    ips_run, power_run, ppw_run  (as defined above)
  from sniper_reconfig_decisions.csv: n_applied (new != prev), Σ transition_penalty_cycles
  row = {bench, arm, cores, interval_insns, instr, runtime_s, energy_j,
         ppw_run, ppw_intervals=[...], n_reconfigs, penalty_cycles, py_mcpat_walltime_s}

tools/reconfig/analyze_final_experiment.py         # or: adapt ppw_savings_summary.py's loader
  per bench: savings_vs_no_change, savings_vs_best_static, headroom_captured
  geomean across benches of each
  plots:
    1. grouped bar: ppw_run by (bench, arm)
    2. scatter: runtime_s (x) vs power_run (y), one point per (bench, arm);
       draw the static Pareto frontier, mark where dynamic_rf lands
    3. bar: savings_vs_best_static % per bench (wins / losses)
    4. one bench, per-interval trace: live L2/L3/BTB (decision log)
       + ppw_i for dynamic_rf vs best_static vs oracle on the same axis
```

## What "our savings are better" looks like in the writeup

- **Primary:** positive geomean `savings_vs_best_static` — dynamic beats the best
  hindsight-chosen fixed config — with the per-benchmark spread and a
  wins/losses count.
- **Framed as headroom:** "dynamic reconfiguration captures X% of the PPW gap
  between the best fixed configuration and the per-interval oracle, online, with
  no per-workload tuning" (from `headroom_captured`).
- **Decomposition (scatter):** is the gain lower power at equal performance,
  faster at equal power, or Pareto-better on both? The `ips**3` weighting makes
  this distinction matter — state which.
- **Cost, stated plainly:** Σ transition-penalty cycles (it *is* in the simulated
  runtime, so already in PPW), number of reconfigurations, and the out-of-band
  Python+McPAT wall time per interval (not in PPW — offline decision latency in
  this model).

## Threats to validity — address each

- **Power definition.** Training PPW uses Processor Runtime Dynamic only. Use the
  same for the headline; leakage-inclusive is a labelled secondary. Never mix.
- **`ips**3` metric.** Disclose it; show raw runtime + energy so a reader who
  wants EDP/ED²P can compute them.
- **Model trained at 2 cores.** Headline claim at **N=2** (`rf_predict.py`, the
  validated `rf_7way` model). N=4 via `rf_predict_ncore.py` is exploratory for
  two independent reasons now, not one: no 3-core ground truth exists AND the
  as-trained surrogate (Pearson r=0.737, `ncore_20260907_002136_metadata.txt`)
  doesn't clear its own 0.8 gate even at 2 cores — `dynamic_rf`'s own simulated
  PPW at N>2 is still real, but nothing derived from `estimate_ppw.py` is.
  Label every N>2 number accordingly.
- **`best_static` uses hindsight.** That is why it, not a random fixed config, is
  the primary comparator — the claim is only meaningful against the strongest
  non-adaptive baseline. `no_change` is the softer, also-reported baseline.
- **Static-sweep-CSV pre-screen could still miss a good config.** It's real
  simulated data (unlike the surrogate, not used for pre-screening at all — see
  Phasing/Static grid above), but it's still a fixed panel, not exhaustive. Also
  run a max-resources and a `base.cfg`-default static arm as sanity checks; if
  either beats the sweep-picked `best_static` in full sim, widen the finalist
  set.
- **Equal work per arm.** `-s stop-by-icount:<N>` or SPLASH ROI markers so
  `total_instructions` is ~constant per benchmark across arms.
- **Warm-up.** Drop interval 0 (cold caches; model sees no deltas) from every
  arm's aggregate.
- **Determinism.** Single-threaded interval model is deterministic; multithreaded
  SPLASH runs need 3 repeats per (bench, arm) with reported spread.
- **McPAT node/voltage/temperature constant** across arms — no cfg arm may touch
  `power/` or technology params.
- **Prefetcher power not modelled** (documented McPAT gap). If `dynamic_rf`'s
  wins correlate with prefetcher switching (check the decision log), that energy
  delta is invisible to McPAT — caveat it.

## Running it on the CentOS 6 branch / container

The container cannot run the RF model or the surrogate (glibc / python3.6 /
sklearn skew — see `RECONFIGURATION_CHANGES.md`).

- Sniper + McPAT run **in the container** for every arm.
- `dynamic_rf` prediction runs **on the host**: start
  `python3 .reconfig_bridge_watch.py` (host, this dir) before the run, cfg has
  `reconfig/python_hook_script = .reconfig_bridge_shim.sh`. For N>2 cores:
  `RECONFIG_PREDICT_SCRIPT=tools/reconfig/rf_predict_ncore.py python3 .reconfig_bridge_watch.py`.
- `no_change` uses a container-local `noop_predict.py` directly — no model, no
  bridge.
- The big `.pkl`s (`rf_7way`'s ~2GB model, and the `ncore_20260907_002136`
  bundle's percore/L3/surrogate models) are **not in the repo** — the small
  `_scaler`/`_imputer`/`_encoder(s)` companions and `_metadata.txt` are
  committed, and both bundles are already copied into
  `tools/reconfig/model/` on this host as of this writing. On a fresh checkout,
  copy the big `.pkl`s in manually before any `dynamic_rf` or `estimate_ppw.py`
  run (source: `snipersim_framework/pythonScripts/saved_models/` for the ncore
  bundle).
- All analysis (`static_vs_optimal.py`, `ppw_savings_summary.py`,
  `estimate_ppw.py`, `analyze_final_experiment.py`, the finalist pre-screen) runs
  on the **host**, offline, against the container run's `sim.stats`,
  `power-*.txt`, and `sniper_reconfig_decisions.csv`.

## Minimum viable version

Phase 1 only: N=2, 4 SPLASH workloads, one interval size (1e6 instr),
`rf_predict.py` + `rf_7way`, arms = {`no_change`, `best_static`, `dynamic_rf`},
1 run each = 12 full Sniper+McPAT runs. `per_interval_oracle` from the existing
static sweep panel (no new sim). Yields the geomean `savings_vs_best_static` /
`savings_vs_no_change` / `headroom_captured` with per-benchmark breakdown — the
core result, and it needs nothing from the still-training N-core models. N=4,
interval sweep, and repeats are Phase 2 / additive.
