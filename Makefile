# aicli - one place every script reaches `claude` and `agy` through.
#
# This repo is the code. Running `server.py` as a service (systemd unit, .env)
# is the deployment's job; homelab's services/AI/aicliAI does it on the OptiPlex.
PYTHON ?= python3
LINK ?= $(HOME)/.local/bin/aicli

.PHONY: help install uninstall test check-quota

help:
	@echo "aicli"
	@echo "====="
	@echo "  make install      - Link $(LINK) -> aicli.py"
	@echo "  make uninstall    - Remove that link"
	@echo "  make test         - Run the unit tests (fake CLI only, no real quota)"
	@echo "  make check-quota  - Verify /usage quota & reset parsing against fixtures and live CLIs"

install:
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
		|| { echo "Error: aicli needs python3 >= 3.11 (tomllib)."; exit 1; }
	@mkdir -p $(dir $(LINK))
	@chmod +x aicli.py server.py
	@ln -sf $(CURDIR)/aicli.py $(LINK)
	@echo "[OK] $(LINK) -> $(CURDIR)/aicli.py"

uninstall:
	@rm -f $(LINK)
	@echo "[OK] removed $(LINK)"

test:
	@$(PYTHON) -m unittest discover -s tests -v

check-quota:
	@$(PYTHON) aicli.py check-quota
