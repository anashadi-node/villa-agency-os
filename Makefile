SHELL := /bin/bash

# Gerbang berjalan di interpreter PRODUKSI, bukan pada `python3` yang kebetulan pertama di PATH.
# Di mesin ini `python3` menunjuk venv Hermes (3.11.16, SQLite 3.53.1) sementara setiap unit
# systemd menjalankan /usr/bin/python3 (3.12.3, SQLite 3.45.1). SQLite yang tertaut adalah sifat
# interpreter, jadi gerbang di venv membuktikan Python yang tidak pernah dipakai produksi — dan
# tes yang bergantung interpreter tetap hijau berbulan-bulan (literal `2_250_000` hanya valid
# sejak SQLite 3.46). Untuk loop cepat, timpa eksplisit: `make test PYTHON=python3`.
PYTHON ?= /usr/bin/python3

# `show`/`status` fall back to data/villa.db when VILLA_DB is unset, so a make variable would
# be silently ignored: `make policy-status VILLA_DB=other.db` would report on the default file
# while looking like it reported on other.db. Export it — but only when it is set, because an
# empty value would override the default with "" and every path would resolve to ".".
ifneq ($(strip $(VILLA_DB)),)
export VILLA_DB
endif

.PHONY: help test verify syntax lint secret-scan unsafe-scan check interpreter schema-parity pg-twin anchor-publish anchor-verify allowance-status policy-status migrate-check render-az audit-structure

help:
	@echo "make test          - run the full pytest suite"
	@echo "make syntax        - byte-compile src/ and tests/"
	@echo "make secret-scan   - scan tracked files for credential patterns"
	@echo "make unsafe-scan   - scan for unsafe config (live-money switches, shuffled ledgers, stored card data)"
	@echo "make schema-parity - fail if the PostgreSQL twin drifted from the SQLite source"
	@echo "make pg-twin       - regenerate src/schema/001_initial.pg.sql from the SQLite source"
	@echo "make lint          - pyflakes over src/ scripts/ tests/ (needs: /usr/bin/python3 -m pip install --user pyflakes)"
	@echo "make anchor-publish- publish the audit-chain anchor (VILLA_DB, VILLA_ANCHOR_DIR)"
	@echo "make anchor-verify - verify the live chain against published anchors (exit 1 = suspect)"
	@echo "make allowance-status- what the §56 medium-risk tool allowance says right now (VILLA_DB)"
	@echo "                     publishing it is deliberately NOT a make target: an owner act"
	@echo "                     needs --tools and --approved-by typed out (see README)"
	@echo "make policy-status - every policy key, and whether an owner has decided it yet (VILLA_DB)"
	@echo "                     setting a value is an owner act too: NOT a make target (see README)"
	@echo "make migrate-check - list schema migrations still pending for VILLA_DB (writes nothing)"
	@echo "                     applying one is an ops act: NOT a make target (see README)"
	@echo "make render-az     - render docs/PROJECT_VILLA_AGENCY_A-Z.md to HTML + PDF in docs/rendered/"
	@echo "make check         - interpreter + syntax + lint + test + secret scan + unsafe scan + schema parity (pre-deploy gate)"

# -q sudah ada di pytest.ini; menambah satu lagi di sini membuatnya -qq, dan pytest 9 tidak
# mencetak baris hitungan pada -qq. Gerbang yang tidak bisa menyebut berapa test yang berjalan
# akan tetap bilang PASS kalau seluruh berkas test diam-diam tidak terkumpul.
test:
	$(PYTHON) -m pytest tests/ --tb=short

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

check: interpreter syntax lint test secret-scan unsafe-scan schema-parity
	@echo "=== PRE-DEPLOY GATE: PASS ==="

# Dicetak paling awal supaya versi interpreter tidak pernah tersembunyi di balik "PASS":
# gerbang yang tidak menyebut interpreter-nya tidak bisa dibedakan dari gerbang di venv.
interpreter:
	@$(PYTHON) -c "import sys, sqlite3; print(f'gate interpreter: Python {sys.version.split()[0]} / SQLite {sqlite3.sqlite_version} ({sys.executable})')"

lint:
	@$(PYTHON) -m pyflakes src scripts tests || { \
		echo "pyflakes: ada temuan di atas, ATAU pyflakes belum terpasang untuk $(PYTHON)."; \
		echo "pasang tanpa sudo (hanya menyentuh ~/.local): /usr/bin/python3 -m pip install --user pyflakes"; exit 1; }

anchor-publish:
	$(PYTHON) scripts/publish_anchor.py publish

anchor-verify:
	$(PYTHON) scripts/publish_anchor.py verify

# Read-only on purpose. Publishing a permission is an owner act that names the tools and the
# approver; a one-keystroke make target would make it possible to open a §56 path by reflex.
allowance-status:
	$(PYTHON) scripts/publish_ai_allowance.py status

# Read-only for the same reason as the allowance: the numbers are the owner's to decide.
# This reports which keys are still waiting; setting one is an owner act, not a target.
policy-status:
	$(PYTHON) scripts/publish_policy_value.py show

# Read-only: applying a migration writes to a database, so it stays an explicit command.
migrate-check:
	$(PYTHON) scripts/apply_migrations.py --dry-run

# Re-render the A-Z document (Markdown -> HTML + PDF). Run it after editing the Markdown:
# a stale PDF that disagrees with the source is a bug, and the renderer used to live in /tmp.
render-az:
	$(PYTHON) scripts/render_az_doc.py

# Read-only structural audit: the checks `check` cannot see (test traceability, documented
# commands that no longer exist, tenant filters, currency-blind money sums, offline assets).
# Deliberately NOT part of `check`: a finding here is a decision for a human, not a deploy stop.
audit-structure:
	$(PYTHON) scripts/audit_structure.py

