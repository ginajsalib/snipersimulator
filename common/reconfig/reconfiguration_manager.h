// Runtime reconfiguration manager - handles RF-model-driven hardware reconfigurations.
// Registered against HOOK_RECONFIGURE (see hooks_manager.h); dispatched once per interval
// from core 0's IntervalPerformanceModel (see interval_performance_model.cc). Dumps live
// stats for cores 0 and 1, runs a Python RF model to predict a new configuration, and
// applies it via CacheCntlr/BranchPredictor. Assumes a 2-core system, matching the trained
// model's _core0/_core1-named feature/target columns.

#ifndef RECONFIGURATION_MANAGER_H
#define RECONFIGURATION_MANAGER_H

#include "fixed_types.h"

#include <string>

class ReconfigurationManager {
private:
   std::string m_stats_output_path;
   std::string m_config_input_path;
   std::string m_python_script_path;

   // Cumulative counters as of the last interval boundary, for computing per-interval
   // deltas (stats.h's recordMetric() returns a running total, not a delta).
   struct CoreCounters {
      UInt64 instructions;
      UInt64 elapsed_time_fs;
      UInt64 branch_incorrect;
      UInt64 l1d_loads, l1d_load_misses;
      UInt64 l2_loads, l2_load_misses;
      UInt64 l3_loads, l3_load_misses;
   };
   CoreCounters m_prev[2];
   bool m_have_prev;

   // Last-applied values that aren't otherwise queryable from the live objects
   // (Cache::getActiveWays() covers current L2/L3 capacity directly, so those aren't
   // tracked here).
   UInt64 m_current_btb_entries[2];
   std::string m_current_prefetch_type[2];

   struct PredictedConfig {
      UInt64 l2_core0_bytes, l2_core1_bytes, l3_bytes;
      UInt64 btb_core0_entries, btb_core1_entries;
      std::string prefetch_core0, prefetch_core1;
   };

   ReconfigurationManager();

public:
   static ReconfigurationManager* getInstance();

   // Read config, seed m_current_btb_entries/m_current_prefetch_type from base config.
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

   static UInt64 readMetric(const char* category, core_id_t core_id, const char* metric);

   // Minimal hand-rolled JSON scalar extraction (no JSON library is linked into this
   // codebase). Returns false if the key isn't found.
   static bool extractJSONNumber(const std::string& content, const std::string& key, UInt64& out);
   static bool extractJSONString(const std::string& content, const std::string& key, std::string& out);
};

#endif
