# cholesky, N=2 — 4-arm comparison

## LATEST (post cache-fix rebuild, dynamic_rf re-run 2026-09-16)

```
PPW[no_change    ] = 3.718e28
PPW[best_static  ] = 4.028e28
PPW[dynamic_rf   ] = 3.485e28      <- was 3.576e28 pre-fix
PPW[max_resources] = 1.668e27

savings_vs_no_change   = -6.28%    <- was -3.85% pre-fix
savings_vs_best_static = -13.47%
```

Provenance: only `dynamic_rf` was re-run on the fixed build; the three static
arms are the 2026-09-13 runs. That is valid here because both fixes are
provable no-ops for a static arm — `noop_predict.py` echoes the current config,
so `setActiveWays()` is always called with the current value and
`m_num_active_ways` never drops below `m_associativity` (making fix 1's added
`isValidReplacement(i)` always true), and with the config never changing fix
2's pre- vs post-apply snapshot is byte-identical.

**The result got worse, and that is the expected direction.** `diagnose` now
reports **1** transition instead of 9:

```
1 actual transitions out of 594 intervals
  interval 1   btb_entries_core0,btb_entries_core1,l3_bytes   -4.2%
of 1 judged decisions: 0 helped, 1 hurt
mean PPW delta, ALL intervals (dynamic_rf - no_change): -6.274e+26
mean PPW gap,  ALL intervals (best_static - dynamic_rf): +2.406e+27
```

The drop from 9 transitions to 1 is itself evidence the refill bug was real:
pre-fix, each L3 shrink was silently undone by allocation back into the gated
ways, so the next interval's identical prediction registered as a *fresh*
transition. With gating actually holding, the model makes one early move
(BTB 512->1024 on both cores, L3 8MB->4MB) and then holds it for 593
intervals. The deficit widened from -3.85% to -6.28% precisely because the
cost of that wrong shrink is now fully realised instead of being masked — i.e.
the pre-fix numbers flattered `dynamic_rf`, as the audit predicted.

## PREVIOUS (pre cache-fix — superseded, kept for the delta above)

Superseded an earlier version of this file: the first attempt's
`no_change`/`best_static`/`max_resources` arms were secretly all running the
real bridge model (see `BUG_static_arm_drift.md`) — this is the re-run with
that bug fixed, confirmed via 0 real reconfig decisions logged for all three
static arms.

## Headline result

| arm          | PPW      | dynamic W | active config |
|--------------|----------|-----------|----------------|
| best_static  | 4.028e28 | 26.78 | L2=1MB/512KB   L3=16MB  BTB=4096/2048 PF=none/none (100%) |
| no_change    | 3.718e28 | 23.80 | L2=256KB/256KB L3=8MB   BTB=512/512   PF=none/none (100%) |
| dynamic_rf   | 3.576e28 | 24.44 | 82.3% at L2=256KB/256KB L3=8MB BTB=1024/512 PF=none/none, drifts to L3=4-7MB the rest |
| max_resources| 1.668e27 | 6.50  | L2=1MB/1MB     L3=16MB  BTB=4096/4096 PF=simple/simple (100%) |

```
savings_vs_no_change   = -3.85%
savings_vs_best_static = -11.23%
sanity check: max_resources (1.668e27) << best_static (4.028e28) -- see below
```

## Two findings, not one

**1. dynamic_rf underperforms both no_change and best_static.** It's not
catastrophic (unlike max_resources, see below), but it's consistently worse:
3.85% below doing nothing, 11.2% below the hindsight-best static config. Its
9 real reconfig decisions (mostly small L3 downsizes from 8MB toward 4-7MB)
appear net-negative — `results_cholesky_n2.md`'s earlier "just changed vs
held steady" PPW split (-72%) still holds for this arm's own data and wasn't
part of what the bug invalidated. The model isn't finding the
L2=1MB/L3=16MB region `best_static` occupies; it stays near its L2=256KB/
L3=8MB starting point and only makes small, apparently-harmful L3 trims.

**2. "throw more resources at it" is actively harmful for cholesky.**
`max_resources` (max L2/L3/BTB + `simple` prefetcher on both cores) is
**24x worse in PPW** than `best_static`, not better — whole-run IPS drops to
2.21e9 vs ~9.6-10.3e9 for every other arm (a ~4.3x slowdown), while dynamic
power actually drops too (6.50W vs 23.8-26.8W elsewhere) even as the
leakage gap widens sharply (24.3W total vs 6.5W dynamic — cores spending far
more time stalled). Best guess: `simple` prefetcher on both cores, combined
with 1MB L2s, saturates shared L3/memory bandwidth with prefetch traffic for
this benchmark's access pattern, tanking IPC. This means the experiment's own
"sanity check" baseline (bigger should never be catastrophically worse) is
itself the most dramatic result in this batch — worth a follow-up run with
`max_resources` using `PF=none/none` instead, to isolate whether it's the
prefetcher specifically or the cache sizes.

## What are the wrong decisions? (`analyze_final_experiment.py diagnose`)

New subcommand: judges each of dynamic_rf's real transitions against
`no_change`'s PPW at the *same interval index* (a real counterfactual, not
just dynamic_rf's own before/after — interval boundaries are
instruction-count-based, so interval `i` means the same program phase across
arms run on the same benchmark/instruction budget).

```
9 actual transitions out of 598 intervals
  interval 1    btb_entries_core0,l3_bytes   -4.2%
  interval 78   l3_bytes                     -2.3%
  interval 85   l3_bytes                     -2.5%
  ... (7 more l3_bytes-only trims, all -2.2% to -2.5%)

of 9 judged decisions: 0 helped, 9 hurt
mean PPW delta on decision intervals:  -4.23e26  (vs no_change, same phase)
mean PPW delta, ALL intervals:         -3.61e27  (vs no_change, same phase)
```

**Every single decision hurt.** But the aggregate "all intervals" delta is
~8x larger than the "decision moment" delta, because interval 1's BTB bump
(core0: 512→1024 entries) is never reverted — its effect persists for the
rest of the run, not just the one row where it happened. Isolated directly
(the 493 intervals where L3 matches `no_change`'s constant 8MB, so BTB is
the *only* difference from `no_change` in effect):

```
mean PPW delta, BTB-only intervals: -4.24e27  (-11.4% of no_change's PPW)
```

**That single early BTB decision, never corrected, is the dominant cost** —
roughly -11.4% PPW sustained across 82% of the run, dwarfing the 8 later
small L3 trims (-2 to -4% each, and only while active). This reframes the
finding: it's not that the model makes many small mistakes, it's that it
makes one early bad call and then never revisits it — worth checking
whether the model considers reverting a change it already made, or only
ever evaluates fresh state against training-time feature ranges without
any signal that "this was already tried and made things worse."

## Before extending to N=8 / a held-out benchmark

- [ ] re-run `max_resources` with prefetch disabled to isolate the cause of
      its collapse (see above) — currently confounds "more cache" with
      "aggressive prefetch on both cores" in one config
- [ ] investigate why the model doesn't move toward the L2=1MB/L3=16MB region
      `best_static` occupies (feature-range / prediction-inspection checks
      from the previous version of this file still apply)
- [ ] re-run barnes's 3 static arms the same way (its `max_resources` dir
      predates the fix too, and is genuine bridge data from a max-resources
      starting point, not a static baseline — still needs real
      no_change/best_static/max_resources)
