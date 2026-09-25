"""Wave 1-A RED tests for Master Spec tenant scope + supplier route.

These tests intentionally encode requirements that the current brownfield schema
does not satisfy yet. They must fail before implementation starts; do not weaken
or xfail them. Source of truth: Villa Agency OS Master Specification §5, §18,
§39, and docs/WAVE0_MIGRATION_TEST_PLAN.md.
"""

from __future__ import annotations

import sqlite3


PRIMARY_BUSINESS_TABLES = (
    "properties",
    "units",
    "policy_versions",
    "rate_cards",
    "availability_evidence",
    "quotes",
    "holds",
    "bookings",
    "cases",
    "invoices",
    "payments",
    "documents",
    "ops_tasks",
    "incidents",
)

SUPPLIER_ROUTE_TABLES = (
    "suppliers",
    "villa_supplier_relationships",
    "contract_versions",
    "rate_versions",
)


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def foreign_keys(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(conn.execute(f"PRAGMA foreign_key_list({table})"))


def test_agencies_table_exists_as_tenant_scope_root(conn):
    """Master Spec requires agency_id scoping, so `agencies` is the root."""
    assert "agencies" in table_names(conn)


def test_primary_business_tables_have_non_nullable_agency_id(conn):
    """Every primary business record must be scoped by agency_id."""
    missing: list[str] = []
    nullable: list[str] = []
    for table in PRIMARY_BUSINESS_TABLES:
        cols = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        if "agency_id" not in cols:
            missing.append(table)
        elif cols["agency_id"][3] != 1:  # PRAGMA table_info: notnull column
            nullable.append(table)

    assert missing == []
    assert nullable == []


def test_supplier_route_tables_exist(conn):
    """Supplier route is first-class truth for contract/rate/bank/payment terms."""
    tables = table_names(conn)
    missing = [table for table in SUPPLIER_ROUTE_TABLES if table not in tables]
    assert missing == []


def test_bookings_reference_villa_supplier_relationship_not_only_unit(conn):
    """Reservation must store the chosen villa-supplier relationship explicitly."""
    booking_cols = column_names(conn, "bookings")
    assert "villa_supplier_relationship_id" in booking_cols

    route_fk = [
        fk for fk in foreign_keys(conn, "bookings")
        if fk[3] == "villa_supplier_relationship_id"
        and fk[2] == "villa_supplier_relationships"
    ]
    assert len(route_fk) == 1
