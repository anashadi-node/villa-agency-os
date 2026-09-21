SHELL := /bin/bash
PYTHON ?= python3

.PHONY: help test verify syntax secret-scan unsafe-scan check schema-parity pg-twin

help:
	@echo "make test          - run the full pytest suite"
	@echo "make syntax        - byte-compile src/ and tests/"
	@echo "make secret-scan   - scan tracked files for credential patterns"
	@echo "make unsafe-scan   - scan for unsafe config (live-money switches, shuffled ledgers, stored card data)"
	@echo "make schema-parity - fail if the PostgreSQL twin drifted from the SQLite source"
	@echo "make pg-twin       - regenerate src/schema/001_initial.pg.sql from the SQLite source"
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
