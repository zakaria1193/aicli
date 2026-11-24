# aicli - one place every script reaches `claude` and `agy` through.
#
# This repo is the code. Running `server.py` as a service (systemd unit, .env)
# is the deployment's job; homelab's services/AI/aicliAI does it on the OptiPlex.
PYTHON ?= python3
LINK ?= $(HOME)/.local/bin/aicli

.PHONY: help install uninstall test test-auth-smoke check-quota skills-update

help:
	@echo "aicli"
	@echo "====="
	@echo "  make install          - Link $(LINK) -> aicli.py, then fetch the latest skills"
	@echo "  make skills-update    - Fetch the latest of every skill in skills/skills.toml (never fails)"
	@echo "  make uninstall        - Remove that link"
	@echo "  make test             - Run the unit tests (fake CLI only, no real quota)"
	@echo "  make test-auth-smoke  - Smoke-test real claude & agy auth/OAuth parsing in an isolated HOME"
	@echo "  make check-quota      - Verify /usage quota & reset parsing against fixtures and live CLIs"

install:
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
		|| { echo "Error: aicli needs python3 >= 3.11 (tomllib)."; exit 1; }
	@mkdir -p $(dir $(LINK))
	@chmod +x aicli.py server.py
	@ln -sf $(CURDIR)/aicli.py $(LINK)
	@echo "[OK] $(LINK) -> $(CURDIR)/aicli.py"
	@$(MAKE) --no-print-directory skills-update

# Run on every install, so every upgrade refreshes the skills. Never fails:
# a source that can't be reached keeps its current copy (or the vendored one).
skills-update:
	@$(PYTHON) aicli.py skills update || echo "[WARN] skills update failed; runs use the vendored copies"

uninstall:
	@rm -f $(LINK)
	@echo "[OK] removed $(LINK)"

test:
	@$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

test-auth-smoke:
	@$(PYTHON) tests/auth_smoke_test.py -v

check-quota:
	@$(PYTHON) aicli.py check-quota
