#ifndef ROB_PERFORMANCE_MODEL_H
#define ROB_PERFORMANCE_MODEL_H

#include "micro_op_performance_model.h"
#include "rob_timer.h"

class RobPerformanceModel : public MicroOpPerformanceModel
{
public:
   RobPerformanceModel(Core *core);
   ~RobPerformanceModel();
protected:
   virtual boost::tuple<uint64_t,uint64_t> simulate(const std::vector<DynamicMicroOp*>& insts);
   virtual void notifyElapsedTimeUpdate();
private:
   RobTimer rob_timer;

   // Runtime reconfiguration tick, mirroring IntervalPerformanceModel's. The hook was
   // originally only in the interval model, which forced every reconfiguration run onto
   // perf_model/core/type = interval while the training sweep (runSniperWithCfg.sh) was
   // collected under type = rob -- a train/serve mismatch that also made the model's
   // rob_timer.* input features structurally unobtainable at runtime.
   bool m_reconfig_enabled;
   UInt64 m_reconfig_interval;
   UInt64 m_interval_insn_count;
};

#endif
