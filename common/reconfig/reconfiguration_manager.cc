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
   , m_have_prev(false)
{
   for (core_id_t c = 0; c <= 1; c++)
   {
      m_prev[c] = CoreCounters();
      m_current_btb_entries[c] = 0;
   }
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

   m_current_btb_entries[0] = Sim()->getCfg()->getIntArray("perf_model/branch_predictor/num_entries", 0);
   m_current_btb_entries[1] = Sim()->getCfg()->getIntArray("perf_model/branch_predictor/num_entries", 1);

   m_current_prefetch_type[0] = Sim()->getCfg()->getStringArray("perf_model/l2_cache/prefetcher", 0).c_str();
   m_current_prefetch_type[1] = Sim()->getCfg()->getStringArray("perf_model/l2_cache/prefetcher", 1).c_str();

   LOG_PRINT("ReconfigurationManager initialized: script=%s, stats=%s, config=%s",
      m_python_script_path.c_str(), m_stats_output_path.c_str(), m_config_input_path.c_str());
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
      return -1;
   }

   PredictedConfig predicted;
   if (!readConfigJSON(m_config_input_path, predicted))
   {
      LOG_PRINT_WARNING("Failed to read predicted config JSON, skipping reconfiguration this interval");
      return -1;
   }

   applyReconfiguration(predicted);

   LOG_PRINT("Reconfiguration completed");
   return 0;
}

UInt64 ReconfigurationManager::readMetric(const char* category, core_id_t core_id, const char* metric)
{
   StatsMetricBase *m = Sim()->getStatsManager()->getMetricObject(category, core_id, metric);
   if (!m)
      return 0;
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

   std::vector<std::string> fields;
   char buf[256];

   for (core_id_t core_id = 0; core_id <= 1; core_id++)
   {
      Core *core = Sim()->getCoreManager()->getCoreFromID(core_id);

      UInt64 instructions      = readMetric("performance_model", core_id, "instruction_count");
      UInt64 elapsed_time_fs   = readMetric("performance_model", core_id, "elapsed_time");
      UInt64 branch_incorrect  = readMetric("branch_predictor", core_id, "num-incorrect");
      UInt64 l1d_loads         = readMetric("L1-D", core_id, "tloads");
      UInt64 l1d_load_misses   = readMetric("L1-D", core_id, "tload-misses");
      UInt64 l2_loads          = readMetric("L2", core_id, "tloads");
      UInt64 l2_load_misses    = readMetric("L2", core_id, "tload-misses");
      UInt64 l3_loads          = readMetric("L3", core_id, "tloads");
      UInt64 l3_load_misses    = readMetric("L3", core_id, "tload-misses");

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

      snprintf(buf, sizeof(buf), "\"ipc_core%d\": %f", core_id, ipc);                       fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"l1_miss_rate_core%d\": %f", core_id, l1_miss_rate);      fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"l2_miss_rate_core%d\": %f", core_id, l2_miss_rate);      fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"l3_miss_rate_core%d\": %f", core_id, l3_miss_rate);      fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"branch_mpki_core%d\": %f", core_id, branch_mpki);        fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"l2core%d_prev\": %llu", core_id, (unsigned long long)l2_bytes_prev);          fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"btbcore%d_prev\": %llu", core_id, (unsigned long long)m_current_btb_entries[core_id]); fields.push_back(buf);
      snprintf(buf, sizeof(buf), "\"prefetcher_core%d_prev\": \"%s\"", core_id, m_current_prefetch_type[core_id].c_str()); fields.push_back(buf);
      if (core_id == 0)
      {
         snprintf(buf, sizeof(buf), "\"l3_prev\": %llu", (unsigned long long)l3_bytes_prev);
         fields.push_back(buf);
      }

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

   fields.push_back("\"active_cores\": 2");

   fprintf(f, "{\n");
   for (size_t i = 0; i < fields.size(); i++)
      fprintf(f, "  %s%s\n", fields[i].c_str(), (i + 1 < fields.size()) ? "," : "");
   fprintf(f, "}\n");

   fclose(f);
   m_have_prev = true;
}

bool ReconfigurationManager::runPythonPrediction(const std::string& script_path)
{
   std::string cmd = "python3 " + script_path;
   int ret = system(cmd.c_str());

   if (ret != 0)
   {
      LOG_PRINT_WARNING("Python prediction script failed with return code %d", ret);
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
   ok = extractJSONNumber(content, "l2_core0", out.l2_core0_bytes) && ok;
   ok = extractJSONNumber(content, "l2_core1", out.l2_core1_bytes) && ok;
   ok = extractJSONNumber(content, "l3", out.l3_bytes) && ok;
   ok = extractJSONNumber(content, "btb_core0", out.btb_core0_entries) && ok;
   ok = extractJSONNumber(content, "btb_core1", out.btb_core1_entries) && ok;
   ok = extractJSONString(content, "prefetch_core0", out.prefetch_core0) && ok;
   ok = extractJSONString(content, "prefetch_core1", out.prefetch_core1) && ok;

   if (!ok)
      LOG_PRINT_WARNING("Predicted config JSON %s missing one or more required fields", config_file.c_str());

   return ok;
}

void ReconfigurationManager::applyReconfiguration(const PredictedConfig& cfg)
{
   for (core_id_t core_id = 0; core_id <= 1; core_id++)
   {
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

      UInt64 l2_bytes = (core_id == 0) ? cfg.l2_core0_bytes : cfg.l2_core1_bytes;
      std::string prefetch_type = (core_id == 0) ? cfg.prefetch_core0 : cfg.prefetch_core1;
      UInt64 btb_entries = (core_id == 0) ? cfg.btb_core0_entries : cfg.btb_core1_entries;

      ParametricDramDirectoryMSI::CacheCntlr *l2 = mm->getCacheCntlrAt(core_id, MemComponent::L2_CACHE);
      if (l2)
      {
         l2->reconfigure(l2_bytes);
         l2->reconfigurePrefetcher(prefetch_type.c_str(), "l2_cache");
      }
      m_current_prefetch_type[core_id] = prefetch_type;

      if (core_id == 0)
      {
         // L3 is shared across the core group; reconfiguring via core 0's controller
         // affects every core sharing that L3 (see CacheCntlr::reconfigure()).
         ParametricDramDirectoryMSI::CacheCntlr *l3 = mm->getCacheCntlrAt(core_id, MemComponent::L3_CACHE);
         if (l3)
            l3->reconfigure(cfg.l3_bytes);
      }

      PerformanceModel *pm = core->getPerformanceModel();
      if (pm && pm->getBranchPredictor())
         pm->getBranchPredictor()->resizeBTB(btb_entries);
      m_current_btb_entries[core_id] = btb_entries;
   }
}
