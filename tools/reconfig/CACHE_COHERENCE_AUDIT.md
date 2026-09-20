# Way-gating vs. cache coherence — audit of `CacheCntlr`/`Cache`/`CacheSet`

Question asked: does runtime way-gating (`Cache::setActiveWays()`, driven by
`CacheCntlr::reconfigure()`) break MSI coherence?

## Answer: coherence is safe

Three independent mechanisms, all verified by reading the code:

1. **Lookup is not gated.** `CacheSet::find()`
   (`common/core/memory_subsystem/cache/cache_set.cc:68-80`) scans all
   `m_associativity` ways, not `m_num_active_ways`. A line sitting in a
   gated-off way is still found on every access — it can never go "missing"
   and be re-fetched into a second way, so no duplicate/stale copy can arise.
2. **Invalidation is not gated.** `CacheSet::invalidate()` (`cache_set.cc:82-94`)
   likewise scans all ways, so directory-driven invalidations and downgrades
   still reach lines in gated ways. No sharer can silently retain a copy the
   directory thinks it invalidated.
3. **Gating never strands live data.** `Cache::setActiveWays()`
   (`cache.cc:188-215`) scans every set, computes the highest occupied way
   index (`max_used_ways`), and clamps the requested shrink up to it — so at
   the moment of the resize, every gated way is already invalid. Consequently
   `CacheCntlr::reconfigure()` (`cache_cntlr.cc:2308+`) performs no flush or
   writeback, and skips no protocol transition, because nothing is evicted.

MSI invariants (single-writer/multiple-reader, directory sharer lists) are not
violated by resizing, in either direction — growth is equally safe, since
un-gated ways only ever held lines that were coherent and reachable all along.

## But the audit found a real bug next door: gating is not enforced on allocation

`isValidReplacement()` (`cache_set.cc:234-249`) correctly refuses gated ways as
victims. However **every** replacement policy short-circuits past it via an
"invalid block first" fast path that never consults it:

```cpp
// cache_set_lru.cc:27-35 -- same shape in plru/nru/mru/nmru/srrip/random
// First try to find an invalid block
for (UInt32 i = 0; i < m_associativity; i++)      // <-- m_associativity, not m_num_active_ways
   if (!m_cache_block_info_array[i]->isValid())
   {
      moveToMRU(i);
      return i;                                    // <-- no isValidReplacement(i) check
   }
```

And by construction (mechanism 3 above) the gated ways **are exactly the
invalid ones** at gating time. So:

- after a shrink to `N` ways, the next miss in an already-warm set (ways
  `[0,N)` all valid) walks the scan up to `i == N` — a gated way — and
  allocates there;
- repeat, and the set is physically back to full occupancy within a handful of
  misses, **silently undoing the shrink**;
- meanwhile `getActiveWays()` still returns `N`, so `writeLiveConfigSnapshot()`
  keeps telling McPAT the cache is `N` ways / smaller KB, and
  `getLiveL2Bytes()` keeps reporting the small size into the decision log;
- and the *next* `setActiveWays()` call now finds `max_used_ways ==
  associativity`, so **every subsequent shrink is clamped to a no-op** — a
  one-way ratchet back to full size.

## Why this matters for the results already collected

Note the invariant "a gated way holds no valid data" is what makes gating
meaningful at all. The clamp establishes it at resize time; nothing enforces
it afterwards. That distinction is the whole bug.

- **The ratchet is the durable damage.** Once a set refills, the next
  `setActiveWays()` sees `max_used_ways == associativity` and clamps — and
  since those ways are legal victims again, normal churn keeps them occupied,
  so every later shrink is a no-op for the rest of the run. Shrink capability
  effectively dies after the first refill.
- **CORRECTION to an earlier draft of this file:** I first wrote that shrink
  PPW is "systematically flattered" (small-cache power billed against
  full-cache hit rates). That overstates it. `m_num_active_ways` is only ever
  written by `setActiveWays()`, so the stale small value survives exactly
  until the next reconfiguration interval, at which point the clamp raises it
  to full associativity and both the live-config snapshot and the decision log
  report the true full size again. The mis-billing window is therefore **~one
  interval per successful shrink**, not the whole run — a real but bounded
  bias, and with so few successful shrinks in the cholesky data it is not
  large enough to explain `dynamic_rf`'s deficit.
- **It plausibly explains the missing L2 movement.** cholesky's `dynamic_rf`
  logged 0/599 L2 changes on both cores while L3 moved 9 times. That is
  *consistent* with the ratchet, but does **not** prove it: the decision log
  records only post-clamp live values, so "model never requested an L2 change"
  and "requested one and it was clamped" are indistinguishable in the CSV.
- **The model's own input is briefly wrong too.** During that one-interval
  window `dumpIntervalStats()` reports `getActiveWays()` = the stale small
  value, so the model is told the L2 is smaller than it physically is when it
  makes the next prediction.
- **The clamp is invisible.** `CacheCntlr::reconfigure()`'s "clamped to N active
  ways" message is `LOG_PRINT_WARNING`, compiled out under `NDEBUG`
  (`common/misc/log.h:77-90`), so none of this surfaced in any run log.

## Separate finding: McPAT bills each interval against the *next* config

Unrelated to way-gating, found while checking the above.
`handleReconfiguration()` (`reconfiguration_manager.cc:115-119`) runs:

```cpp
applyReconfiguration(predicted);   // config changes HERE
logDecision("applied", &predicted);
writeLiveConfigSnapshot();         // snapshot = POST-apply config
triggerPowerSample();              // --partial = the interval that just ELAPSED
```

`triggerPowerSample()` passes `--partial=<prev_marker>:<this_marker>` — the
window that just executed under the **pre**-apply config — together with
`-c <live_config_path>`, which `writeLiveConfigSnapshot()` just wrote from the
**post**-apply config. So McPAT computes per-access energy from the *new*
geometry and multiplies it by the *old* interval's activity counters.

On intervals where nothing changed (the overwhelming majority) the two configs
are identical and this is harmless. It bites exactly on the intervals where a
change was applied — i.e. precisely the rows `analyze_final_experiment.py
diagnose` reports as decisions. For a shrink it applies smaller-structure
per-access energy to the larger config's access counts, understating that
interval's power and so *overstating* its PPW. Direction is clear from the
code; magnitude is unmeasured. Note this makes cholesky's "all 9 decisions
hurt" result, if anything, mildly optimistic.

Fix would be to snapshot the live config *before* `applyReconfiguration()` for
the power sample, and let the new config take effect from the next window.

## Secondary finding: the clamp is global, not per-set

`setActiveWays()` takes a single `max_used_ways` across *all* sets and applies
one uniform `effective_ways` to every set. One set holding a line in a high way
blocks the entire cache from shrinking. Even without the allocation bug above,
this makes shrinks on a warm cache nearly impossible.

## Fix status

**DONE (synced to dockerMnt, needs a rebuild):**
1. *Allocation fast path gated.* Added `&& isValidReplacement(i)` to the
   "first try to find an invalid block" scan in all 7 policies that have one
   (lru, plru, nru, mru, nmru, srrip, random). `isValidReplacement()` already
   encodes "is this way allocatable", so this is a one-token fix per policy and
   is a no-op when nothing is gated (`m_num_active_ways == m_associativity`).
2. *McPAT off-by-one.* `handleReconfiguration()` now calls
   `writeLiveConfigSnapshot()` + `triggerPowerSample()` **before**
   `applyReconfiguration()`, so the elapsed window is billed against the config
   it actually ran under, then re-snapshots after applying so the file at rest
   describes live state.

**NOT DONE — `reconfig/shrink_policy` = clamp|flush.** Design settled (flag,
defaulting to today's `clamp` behaviour), implementation not written. See below.

**NOT FIXED, deliberately:** `cache_set_round_robin.cc:18-23` validates
`m_replacement_index` (the *next* index) but returns `curr_replacement_index`
(the *old* one, unvalidated) — so it can return a gated way, and can recurse
without bound if nothing is valid. Pre-existing; gating makes it reachable.
Not the configured policy here (nehalem/gainestown use `lru`), and the right
fix depends on intended round-robin semantics, so flagging not guessing.

## Implementing the flush policy (design settled, code not written)

`CacheCntlr::reconfigure()` reads `reconfig/shrink_policy`; on `flush` with
`target_ways < current`, walk ways `[target, current)` in every set and evict
each valid line, then `setActiveWays(target)` succeeds unclamped.

Per line, the eviction must reproduce `insertCacheBlock()`'s eviction tail
(`cache_cntlr.cc:1411-1527`). Most of it is reusable:
`updateCacheBlock(addr, CacheState::INVALID, Transition::EVICT, buf, thread_num)`
already back-invalidates every prev-level controller (it rewrites the reason to
`BACK_INVAL` for them), does the local transition and stats, and hands back
dirty data in `buf`. What is **not** covered and must be written is the
disposal branch: next-level `writeCacheBlock()`, or DRAM writeback via
`accessDRAM()`, or the LLC's directory message — `FLUSH_REP` when the line was
MODIFIED, `INV_REP` when SHARED/EXCLUSIVE. The pre-invalidation cstate must be
captured first to choose between them.

Two consequences to settle before this is meaningful:
- **The transition penalty becomes wrong.** `cache_cntlr.h:410-412` justifies a
  fixed `reconfig/transition_penalty_cycles` on the grounds that "nothing is
  ever forcibly evicted". Under `flush` a shrink evicts up to
  `num_sets x ways_removed` lines (~65k for dropping 8 of 16 ways on this L3),
  each potentially a back-invalidation and a writeback. A fixed 100 cycles is
  indefensible there; the penalty needs to scale with lines flushed/dirty.
- **It cannot be trusted without a run.** This is new coherence-protocol code
  in the path where mistakes surface as a deadlock or a wrong-data assert deep
  in a run, and it cannot be compiled or executed from the analysis host — the
  build and run are container-side. It needs a compile, then a short arm under
  `shrink_policy=flush`, before any result from it is quoted.

## Earlier verification suggestion (superseded by the fixes above)

1. **Confirm the ratchet empirically** before fixing: add a temporary counter of
   allocations landing at `index >= m_num_active_ways`, plus an fprintf of the
   requested-vs-effective ways in `setActiveWays()` (raw `fprintf`, since
   `LOG_PRINT*` is compiled out), and run one short arm.
2. **Fix**: gate the fast path — `if (!valid(i) && isValidReplacement(i))` — in
   each policy, or better, bound the scan by `m_num_active_ways` in a shared
   helper so no policy can forget it.
3. **Then decide the intended semantics of a shrink**: with the fast path
   fixed, gated ways stay empty but still *service hits* until their lines
   die naturally. If the intent is that a shrink genuinely costs hit rate
   immediately, the resize needs to invalidate (and write back dirty lines in)
   the gated ways — which is a real coherence operation and would need the
   directory notified, unlike today's no-op.
