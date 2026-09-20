# RESOLVED and VERIFIED: "static" arms were secretly running the real bridge model

Verified end-to-end post-fix (`run_drift_diagnostic.sh`, 4 intervals): every
`l2_bytes_new*`/`btb_entries_new*`/`prefetch_new*`/`l3_bytes_new` column now
matches its `*_prev` counterpart exactly, `status=applied` throughout — true
zero drift. Also confirmed `dynamic_rf`'s existing cholesky data (599
intervals, PPW 3.576e28) is **not** affected by this bug: its
`python_hook_script` was already `.reconfig_bridge_shim.sh`, which is exactly
what the hardcoded `"python3 " + script_path` accidentally invoked anyway.
Only arms whose configured script *wasn't* the shim (`no_change`/
`best_static`/`max_resources`, all pointed at `noop_predict.py`) were broken
— only those three need re-collecting, for both cholesky and barnes.

## Root cause

`/usr/bin/python3` inside the CentOS6 container is a symlink to
`.reconfig_bridge_shim.sh` (needed so the bridge hand-off works for the
`dynamic_rf` arm). But `ReconfigurationManager::runPythonPrediction()`
(`reconfiguration_manager.cc`) hardcoded the invocation as:
```cpp
std::string cmd = "python3 " + script_path;
int ret = system(cmd.c_str());
```
`script_path` (the value of `reconfig/python_hook_script`) was passed only as
an **argument** to `python3` — and since `python3` *is* the shim, and the
shim never inspects its own arguments, `script_path` was silently ignored on
every single invocation, for every arm, always. Confirmed directly: running
`python3 $SNIPER_ROOT/tools/reconfig/noop_predict.py` by hand (bypassing
run-sniper entirely) printed the shim's own
`[reconfig bridge] timed out waiting for host-side predictor` message and
exited 1 — proof `noop_predict.py`'s actual code never ran at all.

## What this means for every run collected so far

Every reconfig-enabled run in this project, regardless of what
`reconfig/python_hook_script` was set to, actually executed the bridge shim
and got answers from whatever the **host watcher** was configured with at
that moment:

- barnes's `max_resources` directory (48/48 intervals "changed") — this
  **was** genuine bridge-driven `dynamic_rf` behavior all along, starting
  from a max-resources initial config. The original pre-compaction
  identification of this directory was correct; my later "it's exactly what
  its name says, just a noop bug" conclusion in an earlier version of this
  file was wrong — retracted.
- cholesky's `no_change`/`best_static`/`max_resources` (this session,
  `run_cholesky_baselines.sh`) — also all silently running the real bridge
  model, just started from three different initial configs. They were never
  static. `results_cholesky_n2.md`'s 4-arm comparison is void: all four
  "arms" are actually the same predictor, differing only in starting point,
  not in whether reconfiguration happened at all.

## Fix applied

1. `reconfiguration_manager.cc`'s `runPythonPrediction()` now runs
   `system(script_path.c_str())` directly — execing the configured script via
   its own shebang + `chmod +x`, instead of hardcoding `python3`. This is
   already synced to dockerMnt; **needs a rebuild**.
2. `noop_predict.py`'s shebang changed from `#!/usr/bin/env python3` (which
   would still resolve back through the same symlink) to the absolute,
   unaliased path `/opt/rh/rh-python36/root/usr/bin/python3.6` (found via
   `find / -iname "python3.*"` inside the container — the Software
   Collections Python 3.6, unrelated to the aliased system `python3`). Made
   executable (`chmod +x`), synced to dockerMnt.
3. `.reconfig_bridge_shim.sh` needs no change — it's still reached correctly
   for `dynamic_rf` (`reconfig/python_hook_script` set to its own path,
   which now actually gets exec'd instead of ignored).

## Verify after rebuild

```bash
# sanity: noop_predict.py now actually runs standalone
python3.6 $SNIPER_ROOT/tools/reconfig/noop_predict.py   # or the full /opt/rh/... path
# (needs a real /tmp/sniper_interval_stats.json present -- see check_noop_predict.sh)

# then re-run the short diagnostic
bash $SNIPER_ROOT/tools/reconfig/run_drift_diagnostic.sh
```
Expect: `status=applied` every interval, with `l2_bytes_new*`/`l3_bytes_new`
identical to `*_prev` on every row (true no-op, zero drift). If that holds,
the `cache_cntlr.cc` debug print (`BUG_static_arm_drift.md`'s earlier
diagnostic) can be reverted, and the entire cholesky 4-arm comparison should
be **re-run from scratch** now that `no_change`/`best_static`/`max_resources`
will actually be static.

## Remaining follow-up

- barnes's `max_resources` dir is real bridge data but from a max-resources
  *starting point*, not a true static max-resources baseline — barnes still
  needs proper `no_change`/`best_static`/`max_resources` runs collected
  post-fix, same as cholesky.
- `NEXT_EXPERIMENTS.md`'s N=8 / held-out-benchmark plan stays paused until
  cholesky's comparison is redone with genuinely-static baselines.
