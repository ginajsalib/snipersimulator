# Plan: N=4 heterogeneous experiment (2 performance + 2 efficient cores)

Status: **plan only, nothing run.** Gated on the N=2 re-run validating the
cache fixes first (see below).

## How heterogeneity is actually expressed (verified, not assumed)

`run-sniper -c <f0>,<f1>,<f2>,<f3>` is Sniper's built-in heterogeneous-core
mechanism (`run-sniper:246-250` -> `make_hetero_config()`, `run-sniper:150-188`):
each listed file becomes one core's config, and run-sniper **transposes** them
into the `key[] = v0,v1,v2,v3` array form that `sniper_config.py:parse_config()`
indexes by core id. It also auto-generates `[tags/core]` entries from each
file's basename.

**Constraint:** `heteroconfig` is *assigned*, not appended — only one
comma-separated `-c` is honoured, a second silently overwrites the first. So
the per-arm L2/BTB/prefetcher settings **cannot** be passed as a separate
`-c core0,core1` the way `run_cholesky_baselines.sh` does today. Each arm must
generate four complete per-core files (core params *and* that arm's cache/BTB/
prefetcher values) and pass them in one `-c c0,c1,c2,c3`.

## Defining "performance" vs "efficient"

Two options:

1. **Reuse Sniper's shipped pair** — `config/cortex-a72.cfg` (big) and
   `config/cortex-a53.cfg` (little). Real, validated big.LITTLE configs.
   *But* they are ARM cores; every row the RF model was trained on is
   Nehalem/Gainestown x86. That swaps the core model wholesale and makes the
   model's input distribution unrecognisable — a much bigger shift than the
   heterogeneity being studied.
2. **Derive both from nehalem** (recommended) — keep `perf_model/core/type =
   interval` and the Nehalem core model for all four cores, and differentiate
   only by microarchitectural width/speed:

   | knob | performance core | efficient core |
   |---|---|---|
   | `perf_model/core/frequency` | 2.66 | 1.20 |
   | `perf_model/core/interval_timer/dispatch_width` | 4 | 2 |
   | `perf_model/core/interval_timer/window_size` | 128 | 32 |
   | `perf_model/l1_dcache/cache_size` | 32 | 16 |
   | `perf_model/l2_cache/cache_size` | 256 | 128 |
   | `perf_model/branch_predictor/num_entries` | 512 | 256 |

   Cores 0,1 = performance; cores 2,3 = efficient.

Option 2 keeps the experiment's single new variable *heterogeneity*, rather
than confounding it with an ISA/core-model change.

## CORRECTION: the byte->way conversion is already per-core correct

An earlier draft of this file claimed the predicted bytes needed a
transformation to be meaningful across asymmetric cores. **That was wrong**,
and the section below is kept only for the observability point at its end.

`reconfigure()` divides by *that core's own* `num_sets`, so the conversion
already expresses "what fraction of this particular cache does N bytes
occupy". A 262144-byte request is 100% of a 256KB L2 and 200% (saturating) of
a 128KB one -- which is arithmetically correct, not a bug: 256KB genuinely is
more than a 128KB cache can hold. Absolute capacity demand is a defensible
semantics, since a working set's size doesn't depend on how big your cache is.

Any ratio-based reinterpretation is also algebraically identical, so it would
change nothing:
`predicted/prev x current_ways == predicted/(num_sets x block_size)`
(because `prev == current_ways x num_sets x block_size`).

What was genuinely missing is **observability**, now fixed: the decision log
gains `l2_bytes_req_core<N>` and `l3_bytes_req` columns recording the model's
raw request next to the effective `_new` values, so saturation and clamping
are visible (`req > new` means it didn't fit). Previously only post-clamp
values were logged and the clamp's own warning was compiled out under NDEBUG.

## Original (partly superseded) note on per-core byte meanings

`CacheCntlr::reconfigure()` converts the model's predicted **bytes** to ways as
`new_capacity_bytes / (num_sets * m_cache_block_size)`, and
`num_sets = cache_size / (associativity * block_size)` differs per core once
the L2s differ. With 8-way/64B lines: a 256KB L2 has 512 sets, a 128KB L2 has
256 sets. The *same* predicted 262144 bytes therefore asks for 8 ways on the
performance core (its full size) but 16 ways on the efficient core — which
`setActiveWays()` caps at its associativity ("can't enable more ways than were
ever allocated").

So an identical prediction is a no-op on one core and a saturating request on
the other. The model emits absolute bytes and has never seen asymmetric cores.
**Log requested-vs-effective ways per core for this experiment** — otherwise
this is invisible (the clamp warning is compiled out under `NDEBUG`, and the
decision log records only post-clamp values).

## Expect the barriers to dominate

`splash2-<bench>-small-4` is **4 threads** (the trailing number is thread
count, not core count — the N=2 runs were 2:1 oversubscribed). At `-n 4` it's
1:1. SPLASH-2 kernels are barrier-synchronised and largely symmetric, so with
two threads on efficient cores **every barrier gates on the slowest core** and
the performance cores idle. Aggregate IPC/PPW will be dominated by the
efficient cores. That is a real effect worth measuring, not a bug — but
predict it up front so it isn't mistaken for one. It also means per-core idle
time becomes an important secondary metric here in a way it wasn't at N=2.

## Benchmarks

Existing four: `barnes`, `cholesky`, `fft`, `radiosity` (all in the model's
training set except cholesky, which is `NCORE_HELD_OUT`).

Two new, confirmed present in `splashrun -l` and outside the training set:
- **`radix`** — integer sort kernel, cache/bandwidth-heavy, power-of-2 thread
  counts; good stress for the L2/L3 knobs.
- **`water.nsq`** — compute-dense N-body with low miss rates; deliberately the
  opposite profile to radix, so the pair brackets the behaviour space.

(`lu.cont`, `fmm`, `raytrace`, `ocean.cont`, `volrend` are the fallbacks.)

## Arms

Same four as N=2 — but note `best_static` **has no basis here**: the sweep that
produced the hindsight-optimal configs was 2-core homogeneous under `-c rob`.
There is no N=4 heterogeneous sweep, so:
- `no_change` — the four-core baseline config, untouched. This is the primary
  comparator; `savings_vs_no_change` is the headline.
- `dynamic_rf` — the real model via the bridge.
- `max_resources` — ceiling sanity check (and after cholesky's result, expect
  it to possibly *lose*; keep `PF=none` variants in mind).
- `best_static` — either drop it, or broadcast the N=2 values and label it
  clearly as a naive transplant, not a hindsight optimum. Do not report
  `savings_vs_best_static` as a headline for N=4.

## Cost, and phasing

6 benchmarks x 4 arms = 24 runs. N=4 with 4 threads will be materially slower
than the N=2 runs (cholesky took ~28 min there); budget 45-90 min each, so
**18-36 hours serially**. Do not launch all 24.

1. **Phase 0 (blocking).** Finish the N=2 cholesky re-run and confirm the two
   cache fixes behave. Do not stack a new experiment on an unvalidated platform
   change — that is exactly how this week's `-c rob` and `python3`-symlink
   problems stayed hidden.
2. **Phase 1.** One benchmark (`cholesky`, since it's held out and we have the
   most N=2 context for it) x 4 arms at N=4 hetero. Validates the whole setup:
   per-core config transposition, McPAT per-core power, bridge at N=4, the
   bytes-to-ways asymmetry above.
3. **Phase 2.** Remaining 5 benchmarks, once Phase 1's numbers are sane.

## Known threats to validity (additive, on top of the existing ones)

This stacks a **third** train/serve mismatch on the two already documented in
`FINAL_EXPERIMENT.md`:
1. training swept under `-c rob`, runs use `interval` (unavoidable — the hook
   only exists in `IntervalPerformanceModel`);
2. trained on 2 cores, run at 4;
3. **new:** trained on homogeneous cores, run on asymmetric ones — and the
   per-core feature vector has no field describing *what kind of core* it is,
   so the model cannot distinguish a performance core from an efficient one
   except indirectly through IPC/miss-rate/MPKI.

Given `dynamic_rf` already trails `no_change` at N=2 homogeneous, the honest
expectation is that it does **worse** here, not better. The value of this
experiment is characterising *how* it fails on asymmetric hardware (does it
push both core types toward the same config? does it starve the efficient
cores?), not demonstrating a gain. Frame it that way in the writeup.
