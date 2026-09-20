# Next experiments: N=8 scale-out + held-out benchmark

## Status check: can we report savings numbers yet?

No. Every run completed so far is a `dynamic_rf` run (barnes's mislabeled
`max_resources` dir, cholesky in flight). Zero `no_change`, `best_static`, or
correctly-labeled `max_resources` runs exist for any benchmark yet.
`analyze_final_experiment.py compare` needs all arms' McPAT + decision-log
output to compute `savings_vs_no_change` / `savings_vs_best_static` /
`headroom_captured`; `/tmp/static_vs_optimal.txt` is a post-hoc sweep-data
panel (static configs vs per-interval oracle from the training sweep) — useful
context, but it never ran `dynamic_rf` itself, so it can't stand in for the
arm-vs-arm comparison. The missing `no_change` run is exactly the "run it
without reconfig" baseline you asked about — still required.

## Plan A — close out Phase 1 minimum viable (prerequisite, do first)

- [ ] cholesky: `no_change`, `best_static` (sweep pick: L2=1024/512 L3=16384
      BTB=4096/2048 PF=none/none), `max_resources` (correctly labeled, under
      the mounted path this time)
- [ ] barnes: same 3 arms (existing `dynamic_rf` output is fine, just needs
      moving out of the mislabeled `max_resources` dir)
- [ ] `analyze_final_experiment.py compare --benchmark cholesky --no-change ... --best-static ... --dynamic-rf ... --max-resources ...`
      → first real headline numbers

## Plan B — N=8 scale-out

1. Model: `randomForestNCoreGPU.py` / `rf_predict_ncore.py` already generalize
   over core count, but training data topped out at N=2 — N=8 predictions are
   extrapolation. Record this explicitly as a Phase 2 caveat in
   `FINAL_EXPERIMENT.md`.
2. Bridge watcher: restart from `dockerMnt/sniperCodeNewBranch-centos6/` with
   `RECONFIG_PREDICT_SCRIPT=/path/to/rf_predict_ncore.py` pointed at the
   `ncore_20260907_002136_*` bundle.
3. `run-sniper -n 8 -c gainestown` (never `-c rob`), extend the
   `runSniperWithCfg.sh` core0.cfg/core1.cfg per-core override pattern to
   core0..core7, plus `reconfig/enabled=true` and per-run
   `live_config_path`/`decision_log_path`/`mcpat_script_path`.
4. Verify the decision-log CSV grows 8 sets of per-core columns and McPAT
   still fires exactly once per interval (that trigger is core-count
   independent).
5. `estimate_ppw.py` will loudly warn at N>2 (surrogate unvalidated there) —
   expected; don't use it to pre-screen N=8, only for full-sim scoring.
6. No N=8 sweep data exists, so there's no real hindsight `best_static` at
   N=8. Recommended: broadcast the N=2 best_static per-core values to all 8
   cores as a naive baseline, clearly caveated as such, rather than running a
   full N=8 sweep (expensive).
7. First N=8 pass, one benchmark only: `dynamic_rf`, `no_change`,
   `max_resources`. Add the naive `best_static` from step 6 once those look
   sane.

## Plan C — held-out benchmark (outside the training sweep set)

Training sweep set was `{barnes, cholesky, fft, radiosity}`. Confirmed valid
SPLASH-2 program names not in that set (`splashrun -l`): `radix`, `lu.cont`,
`lu.ncont`, `fmm`, `raytrace`, `volrend`, `water.nsq`, `water.sp`,
`ocean.cont`, `ocean.ncont`.

Recommend **`radix`**: light kernel, fast to simulate, cache-behavior-heavy
(good stress test for the L2/L3/BTB reconfiguration knobs), power-of-2
thread counts required — fits both N=2 and N=8 cleanly.
Benchmark string: `splash2-radix-small-<n>` (confirm input class scales; drop
to `test` if `small` is too slow at N=8). `lu.cont` is the fallback if radix's
memory-access pattern turns out too degenerate to exercise reconfiguration.

Same 3-arm minimum as Plan B step 7 (`dynamic_rf`, `no_change`,
`max_resources`) — this exercises genuine generalization: the model never
saw radix's stats distribution during training.

## Triage: cholesky dynamic_rf run ended with a post-ROI SIFT crash

Log tail from the run just finished:
```
[STOPBYICOUNT] Ending ROI after 1000000006 instructions (1000000000 requested)
[SNIPER] Leaving ROI after 1688.17 seconds
[SNIPER] Simulated 1000.0M instructions, 278.1M cycles, 3.60 IPC
[SNIPER] Setting instrumentation mode to FAST_FORWARD
[SNIPER] End
[SNIPER] Elapsed time: 1685.77 seconds
[app0] sift_writer.cc:811: Assertion `!response->fail()' failed.
[app0] Pin app terminated abnormally due to signal 6.
```
Reading order: `[SNIPER]` lines are the controller, `[app0]` lines are the Pin
recorder frontend for core-thread 0 — two processes, interleaved/buffered, not
strictly sequential. The controller already printed `Leaving ROI`, the full
instruction/cycle/IPC summary, and `[SNIPER] End` *before* the app0 abort —
i.e. the ROI (the only region reconfiguration/McPAT/decision-log care about)
had already completed and the controller had already reached its own normal
shutdown path. The abort is in `sift_writer.cc`'s `Emulate()`, in the
**post-ROI FAST_FORWARD tail** (cholesky's un-modeled cleanup/output code
after `stop-by-icount` cuts detailed simulation) — a known pattern when the
controller tears down the SIFT connection while the recorder is still
emulating trailing syscalls. It does not, by itself, mean the ROI-phase data
is bad.

It does NOT by itself confirm the run is good either — verify before trusting
it:
- [ ] `echo $?` right after the command (or check `run-sniper`'s own exit
      code) — a nonzero code here means treat the run as failed regardless of
      the above reasoning.
- [ ] `ls $OUTDIR/sim.cfg $OUTDIR/sim.out $OUTDIR/sim.stats.sqlite3` all
      present and non-empty.
- [ ] `ls $OUTDIR/power-*.txt | wc -l` — should be ~48 (one per interval,
      matching cholesky's earlier 48-interval run) or close to
      `1e9 / reconfig_interval_instructions`; a suspiciously low count means
      the crash *did* cut off the tail end of interval sampling.
- [ ] `wc -l $OUTDIR/sniper_reconfig_decisions.csv` and `tail` it — row count
      should roughly match the power-file count above, and the last row
      shouldn't look truncated/partial.
- [ ] `analyze_final_experiment.py summarize --dir $OUTDIR --label cholesky/dynamic_rf`
      — if it parses cleanly and interval count looks sane, the run is usable
      as-is; re-run only if any of the above checks come up short.

## Recommended order (isolate variables one at a time)

1. Finish cholesky's 3 missing arms at N=2 (trained benchmark) →
   first real `compare()` output.
2. `radix` @ N=2 (`dynamic_rf` + `no_change` + `max_resources`) →
   generalization check in isolation.
3. `barnes` or `cholesky` @ N=8 (`dynamic_rf` + `no_change` + `max_resources`) →
   scale check in isolation.
4. `radix` @ N=8 → combined stress test, only once 1–3 look reasonable.
