#ifndef INTERVAL_PERFORMANCE_MODEL_H
#define INTERVAL_PERFORMANCE_MODEL_H

#include "micro_op_performance_model.h"

class IntervalPerformanceModel : public MicroOpPerformanceModel
{
public:
   IntervalPerformanceModel(Core *core, int misprediction_penalty);
   ~IntervalPerformanceModel();

protected:
   virtual boost::tuple<uint64_t,uint64_t> simulate(const std::vector<DynamicMicroOp*>& insts);
   virtual void notifyElapsedTimeUpdate();

private:
   IntervalTimer interval_timer;

   // RF-model-driven runtime reconfiguration: interval-boundary tracking (core 0 only triggers)
   bool m_reconfig_enabled;
   UInt64 m_reconfig_interval;
   UInt64 m_interval_insn_count;
};

#endif
