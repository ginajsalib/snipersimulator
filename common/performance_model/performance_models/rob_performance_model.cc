#include "rob_performance_model.h"
#include "config.hpp"

RobPerformanceModel::RobPerformanceModel(Core *core)
    : MicroOpPerformanceModel(core, !Sim()->getCfg()->getBoolArray("perf_model/core/rob_timer/issue_memops_at_issue", core->getId()))
    , rob_timer(core,
       this,
       m_core_model,
       Sim()->getCfg()->getIntArray("perf_model/branch_predictor/mispredict_penalty", core->getId()),
       Sim()->getCfg()->getIntArray("perf_model/core/interval_timer/dispatch_width", core->getId()),
       Sim()->getCfg()->getIntArray("perf_model/core/interval_timer/window_size", core->getId())
    )
    , m_reconfig_enabled(Sim()->getCfg()->getBoolArray("reconfig/enabled", core->getId()))
    , m_reconfig_interval(Sim()->getCfg()->getIntArray("reconfig/interval_instructions", core->getId()))
    , m_interval_insn_count(0)
{
}

RobPerformanceModel::~RobPerformanceModel()
{
}

boost::tuple<uint64_t,uint64_t> RobPerformanceModel::simulate(const std::vector<DynamicMicroOp*>& insts)
{
   uint64_t ins; SubsecondTime latency;
   boost::tie(ins, latency) = rob_timer.simulate(insts);

   // Same tick as IntervalPerformanceModel::simulate(): only core 0 drives the
   // system-wide reconfiguration, so multiple cores crossing their interval boundary
   // near-simultaneously cannot produce duplicate/racing triggers.
   if (m_reconfig_enabled && getCore()->getId() == 0)
   {
      m_interval_insn_count += ins;
      if (m_interval_insn_count >= m_reconfig_interval)
      {
         triggerReconfigHook();
         m_interval_insn_count = 0;
      }
   }

   return boost::tuple<uint64_t,uint64_t>(ins, SubsecondTime::divideRounded(latency, m_elapsed_time.getPeriod()));
}

void RobPerformanceModel::notifyElapsedTimeUpdate()
{
   rob_timer.synchronize(m_elapsed_time.getElapsedTime());
}
