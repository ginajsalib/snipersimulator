#include "interval_performance_model.h"
#include "config.hpp"

#include <cstdio>

IntervalPerformanceModel::IntervalPerformanceModel(Core *core, int misprediction_penalty)
    : MicroOpPerformanceModel(core, !Sim()->getCfg()->getBoolArray("perf_model/core/interval_timer/issue_memops_at_dispatch", core->getId()))
    , interval_timer(core,
       this,
       m_core_model,
       misprediction_penalty,
       Sim()->getCfg()->getIntArray("perf_model/core/interval_timer/dispatch_width", core->getId()),
       Sim()->getCfg()->getIntArray("perf_model/core/interval_timer/window_size", core->getId()),
       Sim()->getCfg()->getBoolArray("perf_model/core/interval_timer/issue_contention", core->getId())
      )
    , m_reconfig_enabled(Sim()->getCfg()->getBoolArray("reconfig/enabled", core->getId()))
    , m_reconfig_interval(Sim()->getCfg()->getIntArray("reconfig/interval_instructions", core->getId()))
    , m_interval_insn_count(0)
{
}

IntervalPerformanceModel::~IntervalPerformanceModel()
{
   interval_timer.free(); // Free Windows, and remaining DynamicMicroOps, before deleting the allocator
}

boost::tuple<uint64_t,uint64_t> IntervalPerformanceModel::simulate(const std::vector<DynamicMicroOp*>& insts)
{
   boost::tuple<uint64_t,uint64_t> result = interval_timer.simulate(insts);

   // Runtime reconfiguration: only core 0 drives the system-wide reconfiguration tick,
   // avoiding duplicate/racing triggers when multiple cores cross their interval boundary
   // near-simultaneously. Other cores keep running this same code but never trigger.
   if (m_reconfig_enabled && getCore()->getId() == 0)
   {
      m_interval_insn_count += boost::get<0>(result);
      if (m_interval_insn_count >= m_reconfig_interval)
      {
         triggerReconfigHook();
         m_interval_insn_count = 0;
      }
   }

   return result;
}

void IntervalPerformanceModel::notifyElapsedTimeUpdate()
{
   interval_timer.synchronize(m_elapsed_time.getCycleCount());
}
