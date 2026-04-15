# Makefile — install lab to a standard Unix prefix
#
# Usage:
#   make install                         # → ~/.local/bin/lab + ~/.local/lib/lab/
#   make install PREFIX=/usr/local       # system-wide (needs sudo)
#   make install DESTDIR=./pkg PREFIX=/usr # for distro packagers
#   make uninstall
#   make check                           # run the test suite

PREFIX   ?= $(HOME)/.local
LIBDIR   := $(PREFIX)/lib/lab
BINDIR   := $(PREFIX)/bin
DOCDIR   := $(PREFIX)/share/doc/lab

INSTALL  := install
SRC_DIR  := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
VERSION  := $(shell cat $(SRC_DIR)/VERSION 2>/dev/null || echo 0.0.0)

.PHONY: all install install-lib install-bin install-docs uninstall check version

all:
	@echo "lab $(VERSION) — project-local JupyterLab management CLI"
	@echo ""
	@echo "Targets:"
	@echo "  make install          Install to PREFIX=$(PREFIX)"
	@echo "  make uninstall        Remove installed files"
	@echo "  make check            Run the test suite"
	@echo "  make version          Print version"

version:
	@echo $(VERSION)

# ── install ─────────────────────────────────────────────────────

install: install-lib install-bin install-docs
	@echo ""
	@echo "Installed lab $(VERSION) to $(DESTDIR)$(PREFIX)"
	@echo "  Binary:  $(DESTDIR)$(BINDIR)/lab"
	@echo "  Library: $(DESTDIR)$(LIBDIR)/"
	@echo "  Docs:    $(DESTDIR)$(DOCDIR)/"
	@echo ""
	@echo "Quick start:"
	@echo "  cd /path/to/project"
	@echo "  lab kernel add        # create .venv, register kernel"
	@echo "  lab start             # start JupyterLab in background"
	@echo "  lab kernel exec 'print(\"hello\")'"
	@echo "  lab help              # full usage"

install-lib:
	$(INSTALL) -d $(DESTDIR)$(LIBDIR)/bin
	$(INSTALL) -m 755 $(SRC_DIR)/bin/lab $(DESTDIR)$(LIBDIR)/bin/lab
	$(INSTALL) -m 755 $(SRC_DIR)/bin/_lab_kernel.py $(DESTDIR)$(LIBDIR)/bin/_lab_kernel.py
	$(INSTALL) -m 644 $(SRC_DIR)/VERSION $(DESTDIR)$(LIBDIR)/VERSION

install-bin:
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	@# Wrapper that execs the real script so SCRIPT_DIR resolves to LIBDIR/bin
	@printf '#!/bin/sh\nexec "$(LIBDIR)/bin/lab" "$$@"\n' > $(DESTDIR)$(BINDIR)/lab
	chmod 755 $(DESTDIR)$(BINDIR)/lab

install-docs:
	$(INSTALL) -d $(DESTDIR)$(DOCDIR)
	$(INSTALL) -m 644 $(SRC_DIR)/README.md $(DESTDIR)$(DOCDIR)/
	$(INSTALL) -m 644 $(SRC_DIR)/doc/lab.md $(DESTDIR)$(DOCDIR)/
	$(INSTALL) -m 644 $(SRC_DIR)/LICENSE $(DESTDIR)$(DOCDIR)/

# ── uninstall ───────────────────────────────────────────────────

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/lab
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -rf $(DESTDIR)$(DOCDIR)
	@echo "Removed lab from $(DESTDIR)$(PREFIX)"

# ── check ───────────────────────────────────────────────────────

check:
	@bash $(SRC_DIR)/test-lab.sh
