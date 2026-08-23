#ifndef CACHE_SET_H
#define CACHE_SET_H

#include "fixed_types.h"
#include "cache_block_info.h"
#include "cache_base.h"
#include "lock.h"
#include "random.h"
#include "log.h"

#include <cstring>

// Per-cache object to store replacement-policy related info (e.g. statistics),
// can collect data from all CacheSet* objects which are per set and implement the actual replacement policy
class CacheSetInfo
{
   public:
      virtual ~CacheSetInfo() {}
};

// Everything related to cache sets
class CacheSet
{
   public:

      static CacheSet* createCacheSet(String cfgname, core_id_t core_id, String replacement_policy, CacheBase::cache_t cache_type, UInt32 associativity, UInt32 blocksize, CacheSetInfo* set_info = NULL);
      static CacheSetInfo* createCacheSetInfo(String name, String cfgname, core_id_t core_id, String replacement_policy, UInt32 associativity);
      static CacheBase::ReplacementPolicy parsePolicyType(String policy);
      static UInt8 getNumQBSAttempts(CacheBase::ReplacementPolicy, String cfgname, core_id_t core_id);

   protected:
      CacheBlockInfo** m_cache_block_info_array;
      char* m_blocks;
      UInt32 m_associativity;
      // Number of ways currently power-gated "on". Always <= m_associativity (the physically
      // allocated size, which never changes). Runtime reconfiguration adjusts this, not
      // m_associativity, so addressing/allocation-size math elsewhere is unaffected.
      UInt32 m_num_active_ways;
      UInt32 m_blocksize;
      Lock m_lock;

   public:

      CacheSet(CacheBase::cache_t cache_type,
            UInt32 associativity, UInt32 blocksize);
      virtual ~CacheSet();

      UInt32 getBlockSize() { return m_blocksize; }
      UInt32 getAssociativity() { return m_associativity; }
      Lock& getLock() { return m_lock; }

      // Runtime reconfiguration: power-gate ways in place (physical allocation is untouched).
      UInt32 getActiveWays() const { return m_num_active_ways; }
      void setActiveWays(UInt32 num_active_ways) { m_num_active_ways = num_active_ways; }

      void read_line(UInt32 line_index, UInt32 offset, Byte *out_buff, UInt32 bytes, bool update_replacement);
      void write_line(UInt32 line_index, UInt32 offset, Byte *in_buff, UInt32 bytes, bool update_replacement);
      CacheBlockInfo* find(IntPtr tag, UInt32* line_index = NULL);
      bool invalidate(IntPtr& tag);
      void insert(CacheBlockInfo* cache_block_info, Byte* fill_buff, bool* eviction, CacheBlockInfo* evict_block_info, Byte* evict_buff, CacheCntlr *cntlr = NULL);

      CacheBlockInfo* peekBlock(UInt32 way) const { return m_cache_block_info_array[way]; }

      char* getDataPtr(UInt32 line_index, UInt32 offset = 0);
      UInt32 getBlockSize(void) const { return m_blocksize; }

      virtual UInt32 getReplacementIndex(CacheCntlr *cntlr) = 0;
      virtual void updateReplacementIndex(UInt32) = 0;

      bool isValidReplacement(UInt32 index);
};

#endif /* CACHE_SET_H */
