# Runtime Reconfiguration Implementation for SniperSim

## Overview

RF-model-driven runtime reconfiguration: at each interval boundary (driven by core 0),
SniperSim dumps live stats, runs a trained 7-way Random Forest model
(`tools/reconfig/model/rf_7way_config_predictor_20260730_114244.pkl` — not committed to
git, ~2GB; copy it in manually alongside its `_scaler.pkl`/`_encoder.pkl` companions,
which *are* committed) to predict a new hardware configuration, and applies it: L2
capacity per core, shared L3 capacity, BTB entries per core, and prefetcher type per core.

The model's own metadata: ~0.16% exact 7-way match accuracy, 25-50% per-dimension
accuracy. This is a research/experimentation feature, not a tuned production optimizer.

**Assumption**: the system has exactly 2 cores, matching the model's `_core0`/`_core1`
training columns. Only core 0's `IntervalPerformanceModel` triggers reconfiguration (acts
as the system-wide tick); core 1 keeps its own interval counter but never triggers, so two
cores crossing their boundary near-simultaneously can't cause duplicate/racing Python
invocations.

**Cache resize is soft way power-gating, not flush-and-recreate.** Shrinking a cache never
forces eviction of live (valid/dirty) data: `Cache::setActiveWays()` computes the highest
occupied way across all sets and clamps the shrink to at least that value, so a way is only
gated off once it's guaranteed already empty. Growing is capped at the physically
allocated associativity from cache construction (can't enable more ways than were ever
provisioned).

---

## Architecture

```
IntervalPerformanceModel::simulate()  [core 0 only]
  -> accumulate instructions from the (num_insns, latency) tuple already returned
  -> past reconfig/interval_instructions -> PerformanceModel::triggerReconfigHook()
  -> HooksManager::callHooks(HOOK_RECONFIGURE, core_id)
  -> ReconfigurationManager::reconfigHookCallback (registered in Simulator::start()
     only when reconfig/enabled=true)
      1. dumpIntervalStats(): real per-core IPC/miss-rates/branch-MPKI (computed from
         StatsManager deltas) + current L2/L3/BTB/prefetcher config -> /tmp/sniper_interval_stats.json
      2. run tools/reconfig/rf_predict.py
      3. read /tmp/sniper_new_config.json (7 fields); on any failure at any step, skip
         reconfiguration this interval and leave the current config untouched
      4. apply:
         - CacheCntlr::reconfigure(new_capacity_bytes) on L2 (per core) and L3 (shared,
           applied once via core 0's controller -- CacheCntlr proxies for a shared group
           all point at the same CacheMasterCntlr, so this is safe from any proxy)
         - CacheCntlr::reconfigurePrefetcher(new_type, "l2_cache") per core
         - BranchPredictor::resizeBTB(new_entries) per core
```

---

## C++ Changes

### `common/system/hooks_manager.h` / `.cc`
Added `HOOK_RECONFIGURE` to `HookType::hook_type_t` (before `HOOK_TYPES_MAX`) and the
matching string to `hook_type_names[]` (a `static_assert` ties the array size to the enum
count, so both had to change together). Added to `py_hooks.cc`'s `registerHook()` switch
(`hookCallbackInt` group, alongside `HOOK_INSTR_COUNT` etc.) so Python scripts can also
register against it.

### `common/performance_model/performance_model.h` / `.cc`
- `void triggerReconfigHook()` -- calls `Sim()->getHooksManager()->callHooks(HookType::HOOK_RECONFIGURE, (UInt64)m_core->getId())`.
- `void applyReconfigPenalty(SubsecondTime time)` -- public wrapper around the protected
  `incrementElapsedTime()`, so external subsystems (cache controllers) can charge a
  reconfiguration transition penalty without becoming friends of `PerformanceModel`.

### `common/performance_model/performance_models/interval_performance_model.h` / `.cc`
Added `m_reconfig_enabled`, `m_reconfig_interval`, `m_interval_insn_count`. `simulate()`
captures the `(num_insns, latency)` tuple it already returns, accumulates instructions,
and (only when `getCore()->getId() == 0`) calls `triggerReconfigHook()` and resets the
counter once the threshold is crossed.

### `common/core/memory_subsystem/cache/cache_set.h` / `.cc`
Added `m_num_active_ways` (defaults to full `m_associativity`) plus
`getActiveWays()`/`setActiveWays()`. Extended `isValidReplacement()` to also reject
`index >= m_num_active_ways`. Every replacement policy's "find the first invalid way"
fast path (which bypasses `isValidReplacement()`) got a matching bound/guard added:
`cache_set_{lru,mru,nmru,nru,plru,srrip,chirp,random}.cc`. `cache_set_round_robin.cc` and
`cache_set_mplru.cc` needed no changes -- they already funnel every candidate through
`isValidReplacement()` (or fall back to `CacheSetLRU`, which was fixed).

### `common/core/memory_subsystem/cache/cache.h` / `.cc`
`Cache::setActiveWays(UInt32 target_ways) -> UInt32`: scans every set for the highest
occupied way, clamps the target up to that floor and down to `getAssociativity()`, then
calls `CacheSet::setActiveWays()` on every set. Also added `Cache::getActiveWays()`
(reads any set's value, since it's uniform across sets).

### `common/core/memory_subsystem/parametric_dram_directory_msi/cache_cntlr.h` / `.cc`
- `CacheCntlr::reconfigure(UInt64 new_capacity_bytes)`: derives a target way count with
  the number of sets held fixed (`target_ways = new_capacity_bytes / (getNumSets() *
  m_cache_block_size)`), calls `Cache::setActiveWays()` under `m_master->m_cache_lock`,
  and applies a small **fixed** transition penalty (`reconfig/transition_penalty_cycles`)
  via `PerformanceModel::applyReconfigPenalty()` -- not a dirty-line writeback cost, since
  a shrink never forces an eviction.
- `CacheCntlr::reconfigurePrefetcher(String new_type, String configName)`: builds a new
  `Prefetcher*` via the existing `Prefetcher::createPrefetcher()` factory and swaps it
  into `m_master->m_prefetcher`, explicitly `delete`-ing the old one (previously leaked;
  fixed by giving `Prefetcher` a virtual destructor).
- Both methods are safe to call on either the master `CacheCntlr` or any proxy for a
  shared level/group, since they only touch the shared `m_master`.
- `getCacheBlockSize()` moved from private to public (needed by `ReconfigurationManager`
  to compute current cache capacity for the stats dump).

### `common/performance_model/branch_predictor.h`
Added `virtual void resizeBTB(UInt64 new_entries) { }` (no-op default).

### `common/performance_model/branch_predictors/pentium_m_branch_target_buffer.h`
Replaced the old `#define NUM_WAYS 4` / `#define NUM_ENTRIES 512` fixed-size version with
a config-driven one (`initialize(core_id)` reads `perf_model/branch_predictor/num_ways`
and `.../num_entries`, builds `m_ways` as a `std::vector<Way>`) plus a real
`resize(UInt32 new_entries)`. `PentiumMBranchPredictor::resizeBTB()` forwards to it -- this
is a genuine tagged, way-associative BTB, the credible target for RF-driven resizing.
`PentiumMBranchPredictor`'s constructor now calls `m_btb.initialize(core_id)`.

### `common/performance_model/branch_predictors/one_bit_branch_predictor.h` / `.cc`
Added `resizeBTB()` (`m_bits.assign(new_entries, false)`) since `type=one_bit` is
`config/base.cfg`'s *default* predictor -- so `resizeBTB()` has real effect regardless of
which of the two predictor types (`one_bit` or `pentium_m`) an experiment config selects.
`A53BranchPredictor` was left on the no-op default: its resizable table is a PHT indexed
by history register, not a BTB, and its embedded indirect-branch buffer is hardcoded to
256 entries.

### `common/reconfig/reconfiguration_manager.h` / `.cc`
Rewritten from a dead skeleton (never initialized, never registered, hardcoded placeholder
stats) into the real implementation:
- `initialize()`: reads `reconfig/python_hook_script`, seeds current BTB-entries/prefetcher
  tracking from `config/base.cfg`.
- `dumpIntervalStats()`: real per-core IPC/L1-D/L2/L3 miss rates/branch MPKI, computed as
  deltas between successive `StatsManager::getMetricObject(...)->recordMetric()` calls
  (categories: `performance_model`, `branch_predictor`, `L1-D`, `L2`, `L3`), plus current
  config (`Cache::getActiveWays()` for L2/L3 capacity, self-tracked values for BTB
  entries/prefetcher type since those aren't queryable from the live objects). Does *not*
  attempt to replicate the full ~90-column training schema -- the real scaler 0-fills
  anything it doesn't recognize (see `rf_predict.py`), so a smaller, cleanly-computable
  feature set is an accepted, documented simplification.
- `runPythonPrediction()` / `readConfigJSON()`: `system()` + a small hand-rolled JSON
  scalar parser (no JSON library is linked into this codebase).
- `applyReconfiguration()`: calls the new `CacheCntlr`/`BranchPredictor` methods per core
  and level.
- Registered from `common/system/simulator.cc`'s `Simulator::start()`, only when
  `reconfig/enabled` is true.

---

## Configuration (`config/base.cfg`)

```ini
[perf_model/branch_predictor]
...
num_ways = 4       # PentiumMBranchTargetBuffer way-associativity (type=pentium_m only)
num_entries = 512  # PentiumMBranchTargetBuffer entries per way (type=pentium_m only)

[reconfig]
enabled = false
interval_instructions = 1000000
python_hook_script = tools/reconfig/rf_predict.py
decision_log_path = /tmp/sniper_reconfig_decisions.csv
live_config_path = /tmp/sniper_reconfig_live.cfg
transition_penalty_model = fixed
transition_penalty_cycles = 100
```

---

## Python (`tools/reconfig/`)

- **`rf_predict.py`** (rewritten): loads the real 7-way model + scaler + prefetcher
  encoders from `tools/reconfig/model/`, builds a 1-row feature frame from the simulator's
  JSON, aligns it to the scaler's `feature_names_in_` (0-filling anything missing --
  mirrors `predict_config.py`'s batch-inference approach), predicts, and writes
  `l2_core0`/`l2_core1`/`l3`/`btb_core0`/`btb_core1`/`prefetch_core0`/`prefetch_core1` to
  `/tmp/sniper_new_config.json`. Assumes the model's L2/L3 outputs are in KB (converted to
  bytes for the C++ side) -- flagged in-code as an unverified assumption. Falls back to
  writing back the unchanged current config and exiting non-zero on any error.
- **`train_rf.py`**: left as a simple, generic single-output trainer/example matching the
  original 8-feature spec. It does *not* mirror the real 7-way training pipeline
  (`randomForestGPU.py` at `snipersim_framework/pythonScripts/`, which does joint
  multi-output training, hyperparameter search, and PPW-based evaluation) -- retraining
  the real model was out of scope since a trained model already exists.
- **`model/`**: holds the real model + scaler + prefetcher encoders. The model `.pkl`
  itself (~2GB) is gitignored; the small companions are committed.

---

## CentOS 6 host/container bridge (`.reconfig_bridge_shim.sh` / `.reconfig_bridge_watch.py`)

The CentOS 6 container's Python can't run the real RF model at all (old glibc has no
compatible sklearn wheel, and its python3.6 can't unpickle a model saved with sklearn
1.9.0 either). Rather than fighting that, `rf_predict.py` runs unmodified on the **host**,
which already has a close-enough sklearn, via a small file-based request/response handoff
through a directory that's already bind-mounted into the container -- no extra mount or
container restart needed.

- **`.reconfig_bridge_shim.sh`**: set as `reconfig/python_hook_script` for container runs
  (in place of `tools/reconfig/rf_predict.py` directly). Drops the stats file into
  `.reconfig_bridge/stats_request.json` + a `.ready` flag, then polls (0.1s/30s timeout)
  for a `config_response.json` + `.ready` pair and copies it back to
  `/tmp/sniper_new_config.json`.
- **`.reconfig_bridge_watch.py`**: run manually on the **host**, from this directory,
  before starting a container run needing reconfiguration
  (`python3 .reconfig_bridge_watch.py`). Watches for the shim's request, copies it to the
  host's own `/tmp/sniper_interval_stats.json`, runs `tools/reconfig/rf_predict.py`
  natively, and copies its output back through the bridge. On failure it deliberately does
  *not* touch `config_response.ready` -- the container's shim then genuinely times out
  after 30s rather than finding a stale/missing response file.
- Neither script needs a rebuild after edits (`.sh`/`.py`, no compilation) -- just restart
  the watcher process on the host to pick up changes (Python doesn't hot-reload a running
  process).

---

## McPAT power-model integration

Without this, McPAT's power/area numbers reflected the un-reconfigured baseline hardware for
the entire run: `tools/mcpat.py`'s `edit_XML()` reads cache size/associativity from a static
one-time dump of `base.cfg`, never updated mid-run, and BTB capacity was a hardcoded XML
literal (`"18944,8,4,1, 1,3"`), not even config-driven.

- **`ReconfigurationManager::writeLiveConfigSnapshot()`**: writes the currently-live
  L2/L3 size+associativity (from the same `Cache::getActiveWays()`/`getNumSets()`/
  `getCacheBlockSize()` query `dumpIntervalStats()` already does) and per-core BTB entries to
  `reconfig/live_config_path`, in plain `sniper_config` text format, every interval (including
  on prediction/parse failure, so it always reflects genuinely-live state).
- **`ReconfigurationManager::triggerPowerSample()`**: calls
  `StatsManager::recordStats(<time-marker>)` (the C++ equivalent of what
  `scripts/powertrace.py` does from Python via `sim.stats.write()`) and shells out to
  `tools/mcpat.py -c <live_config_path> --partial=<prev>:<this> --no-graph`, writing
  `power-<t0>-<t1>-<duration>.txt`/`.xml` into the run's normal output dir -- the same
  filename convention `powertrace.py` uses, so output is a drop-in analog. Both are called at
  the end of `handleReconfiguration()`, so every McPAT sample corresponds to exactly one
  reconfiguration interval (no independent timer to drift out of sync with
  `reconfig/interval_instructions` -- `scripts/powertrace.py`'s own timer-driven sampling is
  unrelated and shouldn't be enabled at the same time as `reconfig/enabled=true`, since both
  would write overlapping `power-*` files).
- **`tools/mcpat.py`**: `BTB_config`'s capacity term is now computed from
  `perf_model/branch_predictor/num_entries` (`37 * entries`, derived from the original
  18944-byte/512-entry/4-way baseline) instead of hardcoded; block_width/associativity/
  banks/throughput/latency stay fixed.
- **Known gap**: prefetcher type isn't power-modeled at all -- this McPAT version has no XML
  parameter for a configurable prefetcher engine. It stays available in the CSV decision log
  for qualitative correlation only.

---

## Verification performed

- `make -C common` builds clean (no errors, no warnings) after all changes.
- `rf_predict.py` smoke-tested standalone against hand-written stats fixtures: normal
  prediction path (loads the real model, produces a plausible 7-field config), missing
  model fallback, and malformed-input abort all verified.
- Full binary build (Pin frontend, sift recorder) requires the project's Docker build
  image (`benchmarks-root:saved`) -- not available in this environment; source was synced
  to `/home/gina/Desktop/dockerMnt/sniperCodeNewBranch` for that build.
