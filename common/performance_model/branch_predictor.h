#ifndef BRANCH_PREDICTOR_H
#define BRANCH_PREDICTOR_H

#include <iostream>

#include "fixed_types.h"

class BranchPredictor
{
public:
   BranchPredictor();
   BranchPredictor(String name, core_id_t core_id);
   virtual ~BranchPredictor();

   virtual bool predict(bool indirect, IntPtr ip, IntPtr target) = 0;
   virtual void update(bool predicted, bool actual, bool indirect, IntPtr ip, IntPtr target) = 0;

   UInt64 getMispredictPenalty();
   static BranchPredictor* create(core_id_t core_id);

   UInt64 getNumCorrectPredictions() { return m_correct_predictions; }
   UInt64 getNumIncorrectPredictions() { return m_incorrect_predictions; }

   void resetCounters();

   // RF-model-driven runtime reconfiguration: resize the BTB-like structure this predictor
   // owns, if it has one. No-op by default; overridden by predictors with a real, resizable
   // tagged target buffer. Callers should expect a cold-start penalty next interval, since
   // implementations are free to drop existing entries on resize.
   virtual void resizeBTB(UInt64 new_entries) { }

protected:
   void updateCounters(bool predicted, bool actual);

private:
   UInt64 m_correct_predictions;
   UInt64 m_incorrect_predictions;

   static UInt64 m_mispredict_penalty;
};

#endif
