# Makefile for runtime reconfiguration subsystem

LOCAL_SOURCES = \
	reconfiguration_manager.cc

DEPENDENCIES = \
	$(SIM_ROOT)/common/core/memory_subsystem/parametric_dram_directory_msi/cache_cntlr.h \
	$(SIM_ROOT)/common/system/hooks_manager.h \
	$(SIM_ROOT)/common/system/simulator.h

LOCAL_OBJS = $(addprefix $(OBJDIR)/, $(LOCAL_SOURCES:.cc=.o))

LOCAL_CLEAN = $(CLEAN_OBJS) $(CLEAN_DEPENDS)

-include $(DEPENDENCIES)

all: $(LOCAL_OBJS)

clean: local-clean

-include common/Makefile.common
