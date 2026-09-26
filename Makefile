SHELL := /bin/bash
PYTHON ?= python3

.PHONY: help test verify syntax lint secret-scan unsafe-scan check schema-parity pg-twin anchor-publish anchor-verify allowance-status

help:
	@echo "make test          - run the full pytest suite"
	@echo "make syntax        - byte-compile src/ and tests/"
	@echo "make secret-scan   - scan tracked files for credential patterns"
	@echo "make unsafe-scan   - scan for unsafe config (live-money switches, shuffled ledgers, stored card data)"
	@echo "make schema-parity - fail if the PostgreSQL twin drifted from the SQLite source"
	@echo "make pg-twin       - regenerate src/schema/001_initial.pg.sql from the SQLite source"
	@echo "make lint          - pyflakes over src/ scripts/ tests/ (needs: pip install pyflakes)"
	@echo "make anchor-publish- publish the audit-chain anchor (VILLA_DB, VILLA_ANCHOR_DIR)"
	@echo "make anchor-verify - verify the live chain against published anchors (exit 1 = suspect)"
	@echo "make allowance-status- what the §56 medium-risk tool allowance says right now (VILLA_DB)"
	@echo "                     publishing it is deliberately NOT a make target: an owner act"
	@echo "                     needs --tools and --approved-by typed out (see README)"
	@echo "make check         - syntax + test + secret scan + unsafe scan + schema parity (pre-deploy gate)"

test:
	$(PYTHON) -m pytest tests/ -q --tb=short

syntax:
	$(PYTHON) -m compileall -q src tests scripts

secret-scan:
	@bash scripts/secret_scan.sh

unsafe-scan:
	@bash scripts/unsafe_config_scan.sh

schema-parity:
	$(PYTHON) scripts/generate_pg_twin.py --check

pg-twin:
	$(PYTHON) scripts/generate_pg_twin.py --write

check: syntax test secret-scan unsafe-scan schema-parity
	@echo "=== PRE-DEPLOY GATE: PASS ==="

lint:
	@$(PYTHON) -m pyflakes src scripts tests || { \
		echo "pyflakes tidak tersedia/temuan di atas — install dengan: pip install pyflakes"; exit 1; }

anchor-publish:
	$(PYTHON) scripts/publish_anchor.py publish

anchor-verify:
	$(PYTHON) scripts/publish_anchor.py verify

# Read-only on purpose. Publishing a permission is an owner act that names the tools and the
# approver; a one-keystroke make target would make it possible to open a §56 path by reflex.
allowance-status:
	$(PYTHON) scripts/publish_ai_allowance.py status

