#include "simulator.h"
#include "one_bit_branch_predictor.h"

OneBitBranchPredictor::OneBitBranchPredictor(String name, core_id_t core_id, UInt32 size)
   : BranchPredictor(name, core_id)
   , m_bits(size)
{
}

OneBitBranchPredictor::~OneBitBranchPredictor()
{
}

bool OneBitBranchPredictor::predict(bool indirect, IntPtr ip, IntPtr target)
{
   UInt32 index = ip % m_bits.size();
   return m_bits[index];
}

void OneBitBranchPredictor::update(bool predicted, bool actual, bool indirect, IntPtr ip, IntPtr target)
{
   updateCounters(predicted, actual);
   UInt32 index = ip % m_bits.size();
   m_bits[index] = actual;
}

void OneBitBranchPredictor::resizeBTB(UInt64 new_entries)
{
   // Resizing (and re-sizing the modulo-indexed vector) drops prior direction bits for any
   // index that no longer maps the same way, matching the expected cold-start penalty.
   m_bits.assign((size_t)new_entries, false);
}
