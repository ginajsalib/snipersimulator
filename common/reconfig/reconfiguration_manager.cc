// Runtime reconfiguration manager implementation

#include "reconfiguration_manager.h"
#include "simulator.h"
#include "config.hpp"
#include "core.h"
#include "core_manager.h"
#include "performance_model.h"
#include "branch_predictor.h"
#include "stats.h"
#include "dvfs_manager.h"
#include "mem_component.h"
#include "memory_manager.h"
#include "cache_cntlr.h"
#include "cache.h"
#include "log.h"

#include <cstdlib>
#include <set>
#include <string>
#include <cstdio>
#include <cctype>
#include <fstream>
#include <sstream>
#include <vector>

static ReconfigurationManager* g_reconfiguration_manager = NULL;

ReconfigurationManager::ReconfigurationManager()
   : m_stats_output_path("/tmp/sniper_interval_stats.json")
   , m_config_input_path("/tmp/sniper_new_config.json")
   , m_python_script_path("tools/reconfig/rf_predict.py")
   , m_decision_log_path("/tmp/sniper_reconfig_decisions.csv")
   , m_interval_index(0)
   , m_live_config_path("/tmp/sniper_reconfig_live.cfg")
   , m_mcpat_script_path("tools/mcpat.py")
   , m_prev_time_marker("roi-begin")
   , m_prev_time_marker_ns(0)
   , m_have_prev(false)
{
   // Per-core vectors are sized in initialize(), once getTotalCores() is available.
}

ReconfigurationManager* ReconfigurationManager::getInstance()
{
   if (!g_reconfiguration_manager)
      g_reconfiguration_manager = new ReconfigurationManager();
   return g_reconfiguration_manager;
}

void ReconfigurationManager::initialize()
{
   m_python_script_path = Sim()->getCfg()->getString("reconfig/python_hook_script").c_str();
   if (Sim()->getCfg()->hasKey("reconfig/decision_log_path"))
      m_decision_log_path = Sim()->getCfg()->getString("reconfig/decision_log_path").c_str();
   if (Sim()->getCfg()->hasKey("reconfig/live_config_path"))
      m_live_config_path = Sim()->getCfg()->getString("reconfig/live_config_path").c_str();
   if (Sim()->getCfg()->hasKey("reconfig/mcpat_script_path"))
      m_mcpat_script_path = Sim()->getCfg()->getString("reconfig/mcpat_script_path").c_str();

   UInt32 total_cores = Sim()->getConfig()->getTotalCores();
   m_prev.assign(total_cores, CoreCounters());
   m_current_btb_entries.resize(total_cores);
   m_current_prefetch_type.resize(total_cores);
   m_last_snapshot.ipc.resize(total_cores);
   m_last_snapshot.l1_miss_rate.resize(total_cores);
   m_last_snapshot.l2_miss_rate.resize(total_cores);
   m_last_snapshot.l3_miss_rate.resize(total_cores);
   m_last_snapshot.branch_mpki.resize(total_cores);
   m_last_snapshot.l2_bytes_prev.resize(total_cores);
   m_last_snapshot.btb_entries_prev.resize(total_cores);
   m_last_snapshot.prefetch_type_prev.resize(total_cores);

   for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
   {
      m_current_btb_entries[c] = Sim()->getCfg()->getIntArray("perf_model/branch_predictor/num_entries", c);
      m_current_prefetch_type[c] = Sim()->getCfg()->getStringArray("perf_model/l2_cache/prefetcher", c).c_str();
   }

   LOG_PRINT("ReconfigurationManager initialized: script=%s, stats=%s, config=%s, total_cores=%u",
      m_python_script_path.c_str(), m_stats_output_path.c_str(), m_config_input_path.c_str(), total_cores);
}

SInt64 ReconfigurationManager::reconfigHookCallback(UInt64 arg, UInt64 core_id)
{
   ReconfigurationManager *self = (ReconfigurationManager*)arg;
   return self->handleReconfiguration((core_id_t)core_id);
}

SInt64 ReconfigurationManager::handleReconfiguration(core_id_t core_id)
{
   LOG_PRINT("Reconfiguration triggered for core %d", core_id);

   dumpIntervalStats(m_stats_output_path);

   if (!runPythonPrediction(m_python_script_path))
   {
      LOG_PRINT_WARNING("RF prediction failed, skipping reconfiguration this interval");
      logDecision("predict_failed", NULL);
      writeLiveConfigSnapshot();
      triggerPowerSample();
      m_interval_index++;
      return -1;
   }

   PredictedConfig predicted;
   if (!readConfigJSON(m_config_input_path, predicted))
   {
      LOG_PRINT_WARNING("Failed to read predicted config JSON, skipping reconfiguration this interval");
      logDecision("parse_failed", NULL);
      writeLiveConfigSnapshot();
      triggerPowerSample();
      m_interval_index++;
      return -1;
   }

   // Power-sample BEFORE applying: triggerPowerSample()'s --partial window covers the
   // interval that just ELAPSED, which ran under the pre-apply config, so that's the
   // config McPAT must be given via -c. Snapshotting after applyReconfiguration() (as
   // this did originally) billed each interval's activity counters against the *next*
   // interval's geometry -- harmless when nothing changed, but wrong on exactly the
   // intervals where a reconfiguration was applied. See tools/reconfig/CACHE_COHERENCE_AUDIT.md.
   writeLiveConfigSnapshot();
   triggerPowerSample();

   applyReconfiguration(predicted);
   logDecision("applied", &predicted);
   // Re-snapshot so the file at rest describes what is actually live now (the next
   // interval overwrites it with the same values; this is purely so anyone reading
   // m_live_config_path between intervals sees current state, not the previous config).
   writeLiveConfigSnapshot();
   m_interval_index++;

   LOG_PRINT("Reconfiguration completed");
   return 0;
}

void ReconfigurationManager::logDecision(const char* status, const PredictedConfig* cfg)
{
   bool write_header = (m_interval_index == 0);
   FILE *f = fopen(m_decision_log_path.c_str(), write_header ? "w" : "a");
   if (!f)
   {
      LOG_PRINT_WARNING("Cannot open decision log %s for writing", m_decision_log_path.c_str());
      return;
   }

   const IntervalSnapshot &s = m_last_snapshot;
   UInt32 total_cores = (UInt32)s.ipc.size();

   if (write_header)
   {
      fprintf(f, "interval,status,");
      static const char* per_core_stat_names[] = {
         "ipc", "l1_miss_rate", "l2_miss_rate", "l3_miss_rate", "branch_mpki",
         "l2_bytes_prev", "btb_entries_prev", "prefetch_prev",
         "l2_bytes_new", "btb_entries_new", "prefetch_new",
         // Raw model request, before Cache::setActiveWays() clamps/saturates it. Without
         // this the "_new" columns alone cannot distinguish "the model asked to stay at
         // full size" from "the model asked for more than this core physically has and
         // the request saturated" -- and the clamp's own warning is LOG_PRINT_WARNING,
         // compiled out under NDEBUG. Matters most with heterogeneous cores, where the
         // same byte request is a different fraction of each core's cache.
         "l2_bytes_req"
      };
      for (size_t s_i = 0; s_i < sizeof(per_core_stat_names) / sizeof(per_core_stat_names[0]); s_i++)
         for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
            fprintf(f, "%s_core%d,", per_core_stat_names[s_i], c);
      fprintf(f, "l3_bytes_prev,l3_bytes_new,l3_bytes_req\n");
   }

   fprintf(f, "%llu,%s,", (unsigned long long)m_interval_index, status);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%f,", s.ipc[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%f,", s.l1_miss_rate[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%f,", s.l2_miss_rate[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%f,", s.l3_miss_rate[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%f,", s.branch_mpki[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%llu,", (unsigned long long)s.l2_bytes_prev[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%llu,", (unsigned long long)s.btb_entries_prev[c]);
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++) fprintf(f, "%s,", s.prefetch_type_prev[c].c_str());

   if (cfg)
   {
      // l2/l3 "_new": live-queried actual state, not cfg's raw request -- reconfigure()
      // may have clamped a requested shrink (live data in use), so these can differ from
      // what was asked for. btb/prefetch have no such clamping, so cfg's values stand.
      for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
         fprintf(f, "%llu,", (c < (core_id_t)cfg->cores.size()) ? (unsigned long long)getLiveL2Bytes(c) : 0ULL);
      for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
         fprintf(f, "%llu,", (c < (core_id_t)cfg->cores.size()) ? (unsigned long long)cfg->cores[c].btb_entries : 0ULL);
      for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
         fprintf(f, "%s,", (c < (core_id_t)cfg->cores.size()) ? cfg->cores[c].prefetch.c_str() : "");
      // ..._req: what the model actually asked for. Compare against the "_new" columns
      // above to see saturation/clamping (req > new means the request didn't fit).
      for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
         fprintf(f, "%llu,", (c < (core_id_t)cfg->cores.size()) ? (unsigned long long)cfg->cores[c].l2_bytes : 0ULL);
      fprintf(f, "%llu,%llu,%llu\n", (unsigned long long)s.l3_bytes_prev,
              (unsigned long long)getLiveL3Bytes(), (unsigned long long)cfg->l3_bytes);
   }
   else
   {
      // 4 blank per-core groups now (l2_new, btb_new, prefetch_new, l2_req), then
      // l3_bytes_prev with l3_bytes_new and l3_bytes_req left empty. Keep this count in
      // step with per_core_stat_names above or every column after it shifts.
      for (core_id_t c = 0; c < (core_id_t)(4 * total_cores); c++) fprintf(f, ",");
      fprintf(f, "%llu,,\n", (unsigned long long)s.l3_bytes_prev);
   }

   fclose(f);
}

UInt64 ReconfigurationManager::getLiveL2Bytes(core_id_t core_id)
{
   Core *core = Sim()->getCoreManager()->getCoreFromID(core_id);
   ParametricDramDirectoryMSI::MemoryManager *mm = core
      ? dynamic_cast<ParametricDramDirectoryMSI::MemoryManager*>(core->getMemoryManager())
      : NULL;
   if (!mm)
      return 0;
   ParametricDramDirectoryMSI::CacheCntlr *l2 = mm->getCacheCntlrAt(core_id, MemComponent::L2_CACHE);
   if (!l2 || !l2->getCache())
      return 0;
   return (UInt64)l2->getCache()->getActiveWays() * l2->getCache()->getNumSets() * l2->getCacheBlockSize();
}

UInt64 ReconfigurationManager::getLiveL3Bytes()
{
   Core *core = Sim()->getCoreManager()->getCoreFromID(0);
   ParametricDramDirectoryMSI::MemoryManager *mm = core
      ? dynamic_cast<ParametricDramDirectoryMSI::MemoryManager*>(core->getMemoryManager())
      : NULL;
   if (!mm)
      return 0;
   ParametricDramDirectoryMSI::CacheCntlr *l3 = mm->getCacheCntlrAt(0, MemComponent::L3_CACHE);
   if (!l3 || !l3->getCache())
      return 0;
   return (UInt64)l3->getCache()->getActiveWays() * l3->getCache()->getNumSets() * l3->getCacheBlockSize();
}

void ReconfigurationManager::writeLiveConfigSnapshot()
{
   UInt32 total_cores = Sim()->getConfig()->getTotalCores();
   std::vector<UInt64> l2_bytes(total_cores, 0), l2_ways(total_cores, 0);
   UInt64 l3_bytes = 0, l3_ways = 0;

   for (core_id_t core_id = 0; core_id < (core_id_t)total_cores; core_id++)
   {
      Core *core = Sim()->getCoreManager()->getCoreFromID(core_id);
      ParametricDramDirectoryMSI::MemoryManager *mm = core
         ? dynamic_cast<ParametricDramDirectoryMSI::MemoryManager*>(core->getMemoryManager())
         : NULL;
      if (!mm)
         continue;

      ParametricDramDirectoryMSI::CacheCntlr *l2 = mm->getCacheCntlrAt(core_id, MemComponent::L2_CACHE);
      if (l2 && l2->getCache())
      {
         l2_ways[core_id] = l2->getCache()->getActiveWays();
         l2_bytes[core_id] = l2_ways[core_id] * l2->getCache()->getNumSets() * l2->getCacheBlockSize();
      }

      if (core_id == 0)
      {
         ParametricDramDirectoryMSI::CacheCntlr *l3 = mm->getCacheCntlrAt(core_id, MemComponent::L3_CACHE);
         if (l3 && l3->getCache())
         {
            l3_ways = l3->getCache()->getActiveWays();
            l3_bytes = l3_ways * l3->getCache()->getNumSets() * l3->getCacheBlockSize();
         }
      }
   }

   FILE *f = fopen(m_live_config_path.c_str(), "w");
   if (!f)
   {
      LOG_PRINT_WARNING("Cannot open live-config snapshot %s for writing", m_live_config_path.c_str());
      return;
   }

   fprintf(f, "[perf_model/l2_cache]\n");
   fprintf(f, "cache_size[] = ");
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
      fprintf(f, "%s%llu", c ? "," : "", (unsigned long long)(l2_bytes[c] / 1024));
   fprintf(f, "\nassociativity[] = ");
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
      fprintf(f, "%s%llu", c ? "," : "", (unsigned long long)l2_ways[c]);

   fprintf(f, "\n\n[perf_model/l3_cache]\n");
   fprintf(f, "cache_size = %llu\n", (unsigned long long)(l3_bytes / 1024));
   fprintf(f, "associativity = %llu\n", (unsigned long long)l3_ways);

   fprintf(f, "\n[perf_model/branch_predictor]\n");
   fprintf(f, "num_entries[] = ");
   for (core_id_t c = 0; c < (core_id_t)total_cores; c++)
      fprintf(f, "%s%llu", c ? "," : "", (unsigned long long)m_current_btb_entries[c]);
   fprintf(f, "\n");

   fclose(f);
}

void ReconfigurationManager::triggerPowerSample()
{
   UInt64 elapsed_fs = m_prev.empty() ? 0 : m_prev[0].elapsed_time_fs;
   UInt64 now_ns = elapsed_fs / 1000000ULL;

   char marker_buf[32];
   snprintf(marker_buf, sizeof(marker_buf), "%llu", (unsigned long long)now_ns);
   std::string this_marker(marker_buf);

   Sim()->getStatsManager()->recordStats(this_marker.c_str());

   std::string output_dir = Sim()->getCfg()->getString("general/output_dir").c_str();
   UInt64 duration_ns = now_ns - m_prev_time_marker_ns;

   char cmd[2048];
   snprintf(cmd, sizeof(cmd),
      "python2 %s -d %s -o %s/power-%s-%s-%llu -c %s --partial=%s:%s --no-graph",
      m_mcpat_script_path.c_str(), output_dir.c_str(), output_dir.c_str(),
      m_prev_time_marker.c_str(), this_marker.c_str(), (unsigned long long)duration_ns,
      m_live_config_path.c_str(), m_prev_time_marker.c_str(), this_marker.c_str());
   system(cmd);

   m_prev_time_marker = this_marker;
   m_prev_time_marker_ns = now_ns;
}

UInt64 ReconfigurationManager::readMetric(const char* category, core_id_t core_id, const char* metric)
{
   StatsMetricBase *m = Sim()->getStatsManager()->getMetricObject(category, core_id, metric);
   if (!m)
   {
      // Silently returning 0 here hid a real bug for the whole project: the cache
      // counters were requested as "tloads"/"tload-misses", which are not registered
      // names ("loads"/"load-misses" are), so every L1/L2/L3 miss rate fed to the model
      // was identically 0.0 in every interval of every run. LOG_PRINT_WARNING is
      // compiled out under NDEBUG, so use a raw fprintf -- once per (category, metric)
      // -- to make a bad name impossible to miss.
      static std::set<std::string> warned;
      std::string key = std::string(category) + "/" + metric;
      if (warned.insert(key).second)
         fprintf(stderr, "[reconfig] WARNING: stat '%s' is not registered -- "
                         "reading it as 0 (check the metric name)\n", key.c_str());
      return 0;
   }
   return m->recordMetric();
}

void ReconfigurationManager::dumpIntervalStats(const std::string& output_file)
{
   FILE *f = fopen(output_file.c_str(), "w");
   if (!f)
   {
      LOG_PRINT_WARNING("Cannot open stats file %s for writing", output_file.c_str());
      return;
   }

   UInt32 total_cores = Sim()->getConfig()->getTotalCores();
   std::vector<std::string> core_entries;
   char buf[512];

   for (core_id_t core_id = 0; core_id < (core_id_t)total_cores; core_id++)
   {
      Core *core = Sim()->getCoreManager()->getCoreFromID(core_id);

      UInt64 instructions      = readMetric("performance_model", core_id, "instruction_count");
      UInt64 elapsed_time_fs   = readMetric("performance_model", core_id, "elapsed_time");
      UInt64 branch_incorrect  = readMetric("branch_predictor", core_id, "num-incorrect");
      UInt64 l1d_loads         = readMetric("L1-D", core_id, "loads");
      UInt64 l1d_load_misses   = readMetric("L1-D", core_id, "load-misses");
      UInt64 l2_loads          = readMetric("L2", core_id, "loads");
      UInt64 l2_load_misses    = readMetric("L2", core_id, "load-misses");
      UInt64 l3_loads          = readMetric("L3", core_id, "loads");
      UInt64 l3_load_misses    = readMetric("L3", core_id, "load-misses");

      CoreCounters &prev = m_prev[core_id];
      UInt64 d_instructions     = m_have_prev ? (instructions - prev.instructions) : instructions;
      UInt64 d_elapsed_fs       = m_have_prev ? (elapsed_time_fs - prev.elapsed_time_fs) : elapsed_time_fs;
      UInt64 d_branch_incorrect = m_have_prev ? (branch_incorrect - prev.branch_incorrect) : branch_incorrect;
      UInt64 d_l1d_loads        = m_have_prev ? (l1d_loads - prev.l1d_loads) : l1d_loads;
      UInt64 d_l1d_load_misses  = m_have_prev ? (l1d_load_misses - prev.l1d_load_misses) : l1d_load_misses;
      UInt64 d_l2_loads         = m_have_prev ? (l2_loads - prev.l2_loads) : l2_loads;
      UInt64 d_l2_load_misses   = m_have_prev ? (l2_load_misses - prev.l2_load_misses) : l2_load_misses;
      UInt64 d_l3_loads         = m_have_prev ? (l3_loads - prev.l3_loads) : l3_loads;
      UInt64 d_l3_load_misses   = m_have_prev ? (l3_load_misses - prev.l3_load_misses) : l3_load_misses;

      double ipc = 0.0;
      const ComponentPeriod *clock = Sim()->getDvfsManager()->getCoreDomain(core_id);
      if (clock && d_elapsed_fs > 0)
      {
         UInt64 d_cycles = SubsecondTime::divideRounded(SubsecondTime::FS(d_elapsed_fs), *clock);
         if (d_cycles > 0)
            ipc = (double)d_instructions / (double)d_cycles;
      }

      double l1_miss_rate  = d_l1d_loads > 0 ? (double)d_l1d_load_misses / (double)d_l1d_loads : 0.0;
      double l2_miss_rate  = d_l2_loads  > 0 ? (double)d_l2_load_misses  / (double)d_l2_loads  : 0.0;
      double l3_miss_rate  = d_l3_loads  > 0 ? (double)d_l3_load_misses  / (double)d_l3_loads  : 0.0;
      double branch_mpki   = d_instructions > 0 ? (double)d_branch_incorrect * 1000.0 / (double)d_instructions : 0.0;

      // Current (pre-reconfiguration) capacity, read straight from the live cache rather
      // than self-tracked, so the very first interval reports the real base.cfg values.
      UInt64 l2_bytes_prev = 0, l3_bytes_prev = 0;
      ParametricDramDirectoryMSI::MemoryManager *mm = core
         ? dynamic_cast<ParametricDramDirectoryMSI::MemoryManager*>(core->getMemoryManager())
         : NULL;
      if (mm)
      {
         ParametricDramDirectoryMSI::CacheCntlr *l2 = mm->getCacheCntlrAt(core_id, MemComponent::L2_CACHE);
         if (l2 && l2->getCache())
            l2_bytes_prev = (UInt64)l2->getCache()->getActiveWays() * l2->getCache()->getNumSets() * l2->getCacheBlockSize();

         if (core_id == 0)
         {
            ParametricDramDirectoryMSI::CacheCntlr *l3 = mm->getCacheCntlrAt(core_id, MemComponent::L3_CACHE);
            if (l3 && l3->getCache())
               l3_bytes_prev = (UInt64)l3->getCache()->getActiveWays() * l3->getCache()->getNumSets() * l3->getCacheBlockSize();
         }
      }

      m_last_snapshot.ipc[core_id] = ipc;
      m_last_snapshot.l1_miss_rate[core_id] = l1_miss_rate;
      m_last_snapshot.l2_miss_rate[core_id] = l2_miss_rate;
      m_last_snapshot.l3_miss_rate[core_id] = l3_miss_rate;
      m_last_snapshot.branch_mpki[core_id] = branch_mpki;
      m_last_snapshot.l2_bytes_prev[core_id] = l2_bytes_prev;
      m_last_snapshot.btb_entries_prev[core_id] = m_current_btb_entries[core_id];
      m_last_snapshot.prefetch_type_prev[core_id] = m_current_prefetch_type[core_id];
      if (core_id == 0)
         m_last_snapshot.l3_bytes_prev = l3_bytes_prev;

      snprintf(buf, sizeof(buf),
         "{\"core_id\": %d, \"ipc\": %f, \"l1_miss_rate\": %f, \"l2_miss_rate\": %f, "
         "\"l3_miss_rate\": %f, \"branch_mpki\": %f, \"l2_prev\": %llu, \"btb_prev\": %llu, "
         "\"prefetcher_prev\": \"%s\"}",
         core_id, ipc, l1_miss_rate, l2_miss_rate, l3_miss_rate, branch_mpki,
         (unsigned long long)l2_bytes_prev, (unsigned long long)m_current_btb_entries[core_id],
         m_current_prefetch_type[core_id].c_str());
      core_entries.push_back(buf);

      prev.instructions = instructions;
      prev.elapsed_time_fs = elapsed_time_fs;
      prev.branch_incorrect = branch_incorrect;
      prev.l1d_loads = l1d_loads;
      prev.l1d_load_misses = l1d_load_misses;
      prev.l2_loads = l2_loads;
      prev.l2_load_misses = l2_load_misses;
      prev.l3_loads = l3_loads;
      prev.l3_load_misses = l3_load_misses;
   }

   fprintf(f, "{\n  \"cores\": [\n");
   for (size_t i = 0; i < core_entries.size(); i++)
      fprintf(f, "    %s%s\n", core_entries[i].c_str(), (i + 1 < core_entries.size()) ? "," : "");
   fprintf(f, "  ],\n");
   fprintf(f, "  \"l3\": {\"l3_prev\": %llu},\n", (unsigned long long)m_last_snapshot.l3_bytes_prev);
   fprintf(f, "  \"active_cores\": %u\n", total_cores);
   fprintf(f, "}\n");

   fclose(f);
   m_have_prev = true;
}

bool ReconfigurationManager::runPythonPrediction(const std::string& script_path)
{
   // Run script_path directly (its own shebang + chmod +x decide the interpreter) rather
   // than hardcoding "python3 <script_path>". On the CentOS6 container image, /usr/bin/
   // python3 is itself a symlink to .reconfig_bridge_shim.sh (needed so that arm's bridge
   // hand-off works no matter what reconfig/python_hook_script is set to) -- hardcoding
   // "python3" here meant EVERY script_path silently ran the bridge shim instead of itself,
   // regardless of what reconfig/python_hook_script actually named (see
   // tools/reconfig/BUG_static_arm_drift.md). noop_predict.py's shebang points at a real,
   // unaliased interpreter for exactly this reason.
   int ret = system(script_path.c_str());

   if (ret != 0)
   {
      LOG_PRINT_WARNING("Prediction script %s failed with return code %d", script_path.c_str(), ret);
      return false;
   }
   return true;
}

bool ReconfigurationManager::extractJSONNumber(const std::string& content, const std::string& key, UInt64& out)
{
   std::string pattern = "\"" + key + "\"";
   size_t pos = content.find(pattern);
   if (pos == std::string::npos)
      return false;
   size_t colon = content.find(':', pos + pattern.size());
   if (colon == std::string::npos)
      return false;
   size_t start = colon + 1;
   while (start < content.size() && isspace((unsigned char)content[start]))
      start++;
   size_t end = start;
   while (end < content.size() && (isdigit((unsigned char)content[end]) || content[end] == '.' || content[end] == '-'))
      end++;
   if (end == start)
      return false;
   out = (UInt64)strtoull(content.substr(start, end - start).c_str(), NULL, 10);
   return true;
}

bool ReconfigurationManager::extractJSONString(const std::string& content, const std::string& key, std::string& out)
{
   std::string pattern = "\"" + key + "\"";
   size_t pos = content.find(pattern);
   if (pos == std::string::npos)
      return false;
   size_t colon = content.find(':', pos + pattern.size());
   if (colon == std::string::npos)
      return false;
   size_t quote1 = content.find('"', colon + 1);
   if (quote1 == std::string::npos)
      return false;
   size_t quote2 = content.find('"', quote1 + 1);
   if (quote2 == std::string::npos)
      return false;
   out = content.substr(quote1 + 1, quote2 - quote1 - 1);
   return true;
}

bool ReconfigurationManager::extractJSONObject(const std::string& content, const std::string& key, std::string& out)
{
   std::string pattern = "\"" + key + "\"";
   size_t pos = content.find(pattern);
   if (pos == std::string::npos)
      return false;
   size_t colon = content.find(':', pos + pattern.size());
   if (colon == std::string::npos)
      return false;
   size_t brace_start = content.find('{', colon);
   if (brace_start == std::string::npos)
      return false;

   int depth = 0;
   size_t i = brace_start;
   for (; i < content.size(); i++)
   {
      if (content[i] == '{')
         depth++;
      else if (content[i] == '}')
      {
         depth--;
         if (depth == 0) { i++; break; }
      }
   }
   if (depth != 0)
      return false;

   out = content.substr(brace_start, i - brace_start);
   return true;
}

bool ReconfigurationManager::extractJSONArrayObjects(const std::string& content, const std::string& key, std::vector<std::string>& out)
{
   std::string pattern = "\"" + key + "\"";
   size_t pos = content.find(pattern);
   if (pos == std::string::npos)
      return false;
   size_t colon = content.find(':', pos + pattern.size());
   if (colon == std::string::npos)
      return false;
   size_t bracket_start = content.find('[', colon);
   if (bracket_start == std::string::npos)
      return false;
   size_t bracket_end = content.find(']', bracket_start);
   if (bracket_end == std::string::npos)
      return false;
   std::string array_content = content.substr(bracket_start + 1, bracket_end - bracket_start - 1);

   size_t i = 0;
   while (i < array_content.size())
   {
      size_t obj_start = array_content.find('{', i);
      if (obj_start == std::string::npos)
         break;
      int depth = 0;
      size_t j = obj_start;
      for (; j < array_content.size(); j++)
      {
         if (array_content[j] == '{')
            depth++;
         else if (array_content[j] == '}')
         {
            depth--;
            if (depth == 0) { j++; break; }
         }
      }
      if (depth != 0)
         break;
      out.push_back(array_content.substr(obj_start, j - obj_start));
      i = j;
   }
   return !out.empty();
}

bool ReconfigurationManager::readConfigJSON(const std::string& config_file, PredictedConfig& out)
{
   std::ifstream f(config_file.c_str());
   if (!f.is_open())
   {
      LOG_PRINT_WARNING("Cannot open config file %s for reading", config_file.c_str());
      return false;
   }
   std::stringstream buffer;
   buffer << f.rdbuf();
   std::string content = buffer.str();
   f.close();

   bool ok = true;

   std::vector<std::string> core_objs;
   ok = extractJSONArrayObjects(content, "cores", core_objs) && ok;

   UInt32 total_cores = Sim()->getConfig()->getTotalCores();
   out.cores.assign(total_cores, PerCoreConfig());
   for (size_t i = 0; i < core_objs.size(); i++)
   {
      UInt64 core_id_num = 0;
      bool core_ok = extractJSONNumber(core_objs[i], "core_id", core_id_num);
      if (!core_ok || core_id_num >= total_cores)
      {
         LOG_PRINT_WARNING("Predicted config JSON has a cores[] entry with missing/out-of-range core_id");
         ok = false;
         continue;
      }
      PerCoreConfig &pc = out.cores[core_id_num];
      core_ok = extractJSONNumber(core_objs[i], "l2_bytes", pc.l2_bytes) && core_ok;
      core_ok = extractJSONNumber(core_objs[i], "btb_entries", pc.btb_entries) && core_ok;
      core_ok = extractJSONString(core_objs[i], "prefetch", pc.prefetch) && core_ok;
      ok = core_ok && ok;
   }

   std::string l3_obj;
   ok = extractJSONObject(content, "l3", l3_obj) && ok;
   if (!l3_obj.empty())
      ok = extractJSONNumber(l3_obj, "l3_bytes", out.l3_bytes) && ok;

   if (!ok)
      LOG_PRINT_WARNING("Predicted config JSON %s missing one or more required fields", config_file.c_str());

   return ok;
}

void ReconfigurationManager::applyReconfiguration(const PredictedConfig& cfg)
{
   UInt32 total_cores = Sim()->getConfig()->getTotalCores();
   for (core_id_t core_id = 0; core_id < (core_id_t)total_cores; core_id++)
   {
      if ((size_t)core_id >= cfg.cores.size())
         continue;

      Core *core = Sim()->getCoreManager()->getCoreFromID(core_id);
      if (!core)
         continue;

      ParametricDramDirectoryMSI::MemoryManager *mm =
         dynamic_cast<ParametricDramDirectoryMSI::MemoryManager*>(core->getMemoryManager());
      if (!mm)
      {
         LOG_PRINT_WARNING("reconfig: core %d's memory manager isn't the parametric_dram_directory_msi protocol; skipping", core_id);
         continue;
      }

      const PerCoreConfig &pc = cfg.cores[core_id];

      ParametricDramDirectoryMSI::CacheCntlr *l2 = mm->getCacheCntlrAt(core_id, MemComponent::L2_CACHE);
      if (l2)
      {
         l2->reconfigure(pc.l2_bytes);
         l2->reconfigurePrefetcher(pc.prefetch.c_str(), "l2_cache");
      }
      m_current_prefetch_type[core_id] = pc.prefetch;

      if (core_id == 0)
      {
         // L3 is treated as a single instance shared by every core; reconfiguring via
         // core 0's controller affects every core sharing that L3 (see
         // CacheCntlr::reconfigure()). Systems with multiple independent L3 domains
         // aren't handled — see reconfiguration_manager.h.
         ParametricDramDirectoryMSI::CacheCntlr *l3 = mm->getCacheCntlrAt(core_id, MemComponent::L3_CACHE);
         if (l3)
            l3->reconfigure(cfg.l3_bytes);
      }

      PerformanceModel *pm = core->getPerformanceModel();
      if (pm && pm->getBranchPredictor())
         pm->getBranchPredictor()->resizeBTB(pc.btb_entries);
      m_current_btb_entries[core_id] = pc.btb_entries;
   }
}
