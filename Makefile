# Makefile — install labsh to a standard Unix prefix
#
# Usage:
#   make install                         # → ~/.local/bin/labsh + ~/.local/lib/labsh/
#   make install PREFIX=/usr/local       # system-wide (needs sudo)
#   make install DESTDIR=./pkg PREFIX=/usr # for distro packagers
#   make uninstall
#   make check                           # run the test suite

PREFIX   ?= $(HOME)/.local
LIBDIR   := $(PREFIX)/lib/labsh
BINDIR   := $(PREFIX)/bin
DOCDIR   := $(PREFIX)/share/doc/labsh
CMDDIR   := $(PREFIX)/lib/labsh/commands
CLAUDE_COMMANDS := $(HOME)/.claude/commands

INSTALL  := install
SRC_DIR  := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
VERSION  := $(shell cat $(SRC_DIR)/VERSION 2>/dev/null || echo 0.0.0)

.PHONY: all install install-lib install-bin install-docs install-skill uninstall check version

all:
	@echo "labsh $(VERSION) — project-local JupyterLab management CLI"
	@echo ""
	@echo "Targets:"
	@echo "  make install          Install to PREFIX=$(PREFIX)"
	@echo "  make uninstall        Remove installed files"
	@echo "  make check            Run the test suite"
	@echo "  make version          Print version"

version:
	@echo $(VERSION)

# ── install ─────────────────────────────────────────────────────

install: install-lib install-bin install-docs install-skill
	@echo ""
	@echo "Installed labsh $(VERSION) to $(DESTDIR)$(PREFIX)"
	@echo "  Binary:  $(DESTDIR)$(BINDIR)/labsh"
	@echo "  Library: $(DESTDIR)$(LIBDIR)/"
	@echo "  Docs:    $(DESTDIR)$(DOCDIR)/"
	@echo "  Skill:   $(CLAUDE_COMMANDS)/labsh.md"
	@echo ""
	@echo "Quick start:"
	@echo "  cd /path/to/project"
	@echo "  labsh kernel add        # create .venv, register kernel"
	@echo "  labsh start             # start JupyterLab in background"
	@echo "  labsh kernel exec 'print(\"hello\")'"
	@echo "  labsh help              # full usage"

install-lib:
	$(INSTALL) -d $(DESTDIR)$(LIBDIR)/bin
	$(INSTALL) -m 755 $(SRC_DIR)/bin/labsh $(DESTDIR)$(LIBDIR)/bin/labsh
	$(INSTALL) -m 755 $(SRC_DIR)/bin/_labsh_kernel.py $(DESTDIR)$(LIBDIR)/bin/_labsh_kernel.py
	$(INSTALL) -m 644 $(SRC_DIR)/VERSION $(DESTDIR)$(LIBDIR)/VERSION

install-bin:
	$(INSTALL) -d $(DESTDIR)$(BINDIR)
	@# Wrapper that execs the real script so SCRIPT_DIR resolves to LIBDIR/bin
	@printf '#!/bin/sh\nexec "$(LIBDIR)/bin/labsh" "$$@"\n' > $(DESTDIR)$(BINDIR)/labsh
	chmod 755 $(DESTDIR)$(BINDIR)/labsh

install-docs:
	$(INSTALL) -d $(DESTDIR)$(DOCDIR)
	$(INSTALL) -m 644 $(SRC_DIR)/README.md $(DESTDIR)$(DOCDIR)/
	$(INSTALL) -m 644 $(SRC_DIR)/doc/labsh.md $(DESTDIR)$(DOCDIR)/
	$(INSTALL) -m 644 $(SRC_DIR)/LICENSE $(DESTDIR)$(DOCDIR)/

install-skill:
	$(INSTALL) -d $(DESTDIR)$(CMDDIR)
	$(INSTALL) -m 644 $(SRC_DIR)/commands/labsh.md $(DESTDIR)$(CMDDIR)/labsh.md
	@# Symlink into Claude Code user commands (creates dir if needed)
	@mkdir -p $(CLAUDE_COMMANDS)
	@ln -sfn $(DESTDIR)$(CMDDIR)/labsh.md $(CLAUDE_COMMANDS)/labsh.md
	@echo "  Skill:   $(CLAUDE_COMMANDS)/labsh.md → $(DESTDIR)$(CMDDIR)/labsh.md"

# ── uninstall ───────────────────────────────────────────────────

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/labsh
	rm -rf $(DESTDIR)$(LIBDIR)
	rm -rf $(DESTDIR)$(DOCDIR)
	rm -f $(CLAUDE_COMMANDS)/labsh.md
	@echo "Removed labsh from $(DESTDIR)$(PREFIX)"

# ── check ───────────────────────────────────────────────────────

check:
	@bash $(SRC_DIR)/test-labsh.sh
