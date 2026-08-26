// Runtime reconfiguration manager - handles RF-model-driven hardware reconfigurations.
// Registered against HOOK_RECONFIGURE (see hooks_manager.h); dispatched once per interval
// from core 0's IntervalPerformanceModel (see interval_performance_model.cc). Dumps live
// stats for every core (Sim()->getConfig()->getTotalCores(), not hardcoded), runs a Python
// RF model to predict a new configuration, and applies it via CacheCntlr/BranchPredictor.
// L2/BTB/prefetcher are per-core; L3 is treated as a single instance shared by all cores
// (applied via core 0's CacheCntlr, same as every other core sharing that L3 gets it) —
// systems with multiple independent L3 domains aren't handled by this generalization.

#ifndef RECONFIGURATION_MANAGER_H
#define RECONFIGURATION_MANAGER_H

#include "fixed_types.h"

#include <string>
#include <vector>

class ReconfigurationManager {
private:
   std::string m_stats_output_path;
   std::string m_config_input_path;
   std::string m_python_script_path;
   std::string m_decision_log_path;
   UInt64 m_interval_index;

   // Cumulative counters as of the last interval boundary, for computing per-interval
   // deltas (stats.h's recordMetric() returns a running total, not a delta). Indexed by
   // core_id; sized to getTotalCores() in initialize().
   struct CoreCounters {
      UInt64 instructions;
      UInt64 elapsed_time_fs;
      UInt64 branch_incorrect;
      UInt64 l1d_loads, l1d_load_misses;
      UInt64 l2_loads, l2_load_misses;
      UInt64 l3_loads, l3_load_misses;
   };
   std::vector<CoreCounters> m_prev;
   bool m_have_prev;

   // Last-applied values that aren't otherwise queryable from the live objects
   // (Cache::getActiveWays() covers current L2/L3 capacity directly, so those aren't
   // tracked here). Indexed by core_id.
   std::vector<UInt64> m_current_btb_entries;
   std::vector<std::string> m_current_prefetch_type;

   struct PerCoreConfig {
      UInt64 l2_bytes;
      UInt64 btb_entries;
      std::string prefetch;
   };
   struct PredictedConfig {
      std::vector<PerCoreConfig> cores; // indexed by core_id
      UInt64 l3_bytes;
   };

   // Pre-reconfiguration snapshot captured by dumpIntervalStats(), retained so
   // logDecision() can pair "before" stats with the "after" (predicted) config
   // in a single CSV row without re-reading the live objects. Indexed by core_id.
   struct IntervalSnapshot {
      std::vector<double> ipc, l1_miss_rate, l2_miss_rate, l3_miss_rate, branch_mpki;
      std::vector<UInt64> l2_bytes_prev;
      std::vector<UInt64> btb_entries_prev;
      std::vector<std::string> prefetch_type_prev;
      UInt64 l3_bytes_prev;
   };
   IntervalSnapshot m_last_snapshot;

   ReconfigurationManager();

public:
   static ReconfigurationManager* getInstance();

   // Read config, size the per-core vectors to getTotalCores(), and seed
   // m_current_btb_entries/m_current_prefetch_type from base config.
   void initialize();

   // Hook callback (static) that dispatches to the singleton instance.
   static SInt64 reconfigHookCallback(UInt64 arg, UInt64 core_id);

   // Run one full reconfiguration cycle. Returns 0 on success, -1 on any failure (in which
   // case the current configuration is left untouched — no partial reconfiguration).
   SInt64 handleReconfiguration(core_id_t core_id);

private:
   void dumpIntervalStats(const std::string& output_file);
   bool runPythonPrediction(const std::string& script_path);
   bool readConfigJSON(const std::string& config_file, PredictedConfig& out);
   void applyReconfiguration(const PredictedConfig& cfg);

   // Appends one row to m_decision_log_path pairing m_last_snapshot ("before") with
   // cfg ("after"); cfg is NULL when prediction/parsing failed and no change was applied.
   void logDecision(const char* status, const PredictedConfig* cfg);

   static UInt64 readMetric(const char* category, core_id_t core_id, const char* metric);

   // Minimal hand-rolled JSON extraction (no JSON library is linked into this codebase).
   // Return false if the key isn't found. The Object/ArrayObjects variants return raw
   // substrings (matched by brace depth) so the scalar extractors can be re-run on them.
   static bool extractJSONNumber(const std::string& content, const std::string& key, UInt64& out);
   static bool extractJSONString(const std::string& content, const std::string& key, std::string& out);
   static bool extractJSONObject(const std::string& content, const std::string& key, std::string& out);
   static bool extractJSONArrayObjects(const std::string& content, const std::string& key, std::vector<std::string>& out);
};

#endif
