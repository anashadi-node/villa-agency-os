"""Bulk inventory import: the file is the contract, so the tests attack the file.

A bulk import fails in three ways worth testing, none of them the happy path: the *shape* is wrong
(an unknown column, a missing one), a *row* is wrong (and the refusal must name the line), and the
*write* is wrong (a half-imported list, a duplicate on re-run, a row that reached across agencies).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import reservation_kernel as RK
import villa_import as VI
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-10-02T08:00:00+00:00"
AGENCY = "agency_default"
SCHEMA = Path(__file__).resolve().parents[1] / "src" / "schema" / "001_initial.sql"

HEADER = (
    "property_id,property_name,property_type,region,province,timezone,property_status,"
    "contract_pack_status,latitude,longitude,geo_precision,geo_source,"
    "unit_id,unit_name,unit_type,bedrooms,bathrooms,base_capacity,"
    "max_guests,unit_status"
)


def row(**overrides) -> str:
    base = {
        "property_id": "villa_ubud_01",
        "property_name": "Villa Ubud Satu",
        "property_type": "villa",
        "region": "Ubud",
        "province": "Bali",
        "timezone": "Asia/Makassar",
        "property_status": "SELLABLE",
        "contract_pack_status": "ACTIVATED",
        "latitude": "",
        "longitude": "",
        "geo_precision": "",
        "geo_source": "",
        "unit_id": "unit_ubud_01",
        "unit_name": "Unit Utama",
        "unit_type": "entire_villa",
        "bedrooms": "2",
        "bathrooms": "2",
        "base_capacity": "2",
        "max_guests": "4",
        "unit_status": "ACTIVE",
    }
    base.update(overrides)
    return ",".join(str(base[column]) for column in VI.COLUMNS)


def file_of(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


@pytest.fixture()
def kernel():
    instance = RK.ReservationKernel.open(
        ":memory:",
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed="villa-import"),
        agency_id=AGENCY,
    )
    yield instance
    instance.close()


# --------------------------------------------------------------------------- #
# 1. The file shape is checked before the values
# --------------------------------------------------------------------------- #
def test_an_unknown_column_is_refused_and_named(kernel):
    bad = HEADER + ",villa_manager"
    with pytest.raises(VI.InvalidImportFile) as caught:
        VI.plan_import(kernel, file_of(row() + ",Pak Budi", header=bad))
    assert "villa_manager" in str(caught.value.details.get("columns", ""))


def test_a_missing_required_column_is_refused_and_named(kernel):
    bad = HEADER.replace("base_capacity,", "")
    with pytest.raises(VI.InvalidImportFile) as caught:
        VI.plan_import(kernel, file_of(row(), header=bad))
    assert "base_capacity" in str(caught.value.details.get("columns", ""))


def test_a_repeated_column_in_the_header_is_refused(kernel):
    with pytest.raises(VI.InvalidImportFile):
        VI.plan_import(kernel, file_of(row(), header=HEADER + ",unit_id"))


def test_a_header_with_no_rows_is_refused(kernel):
    with pytest.raises(VI.InvalidImportFile):
        VI.plan_import(kernel, HEADER + "\n")


def test_a_trailing_blank_line_is_not_a_row(kernel):
    plan = VI.plan_import(kernel, file_of(row()) + "\n,\n")
    assert len(plan.rows) == 1


# --------------------------------------------------------------------------- #
# 2. Validation names the line
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"timezone": "Asia/Ubud"}, "timezone"),
        ({"property_type": "castle"}, "property_type"),
        ({"province": "Jawa Tengah"}, "province"),
        ({"unit_status": "DIRTY"}, "unit_status"),
        ({"property_status": "OK"}, "property_status"),
        ({"bedrooms": "dua"}, "bedrooms"),
        ({"max_guests": "1"}, "max_guests"),
        ({"region": "U"}, "region"),
        ({"unit_name": "X" * 121}, "unit_name"),
        ({"unit_id": "01"}, "unit_id"),
        ({"property_name": ""}, "property_name"),
    ],
)
def test_a_bad_value_is_refused_with_its_line_and_field(kernel, overrides, field):
    plan = VI.plan_import(kernel, file_of(row(**overrides)))
    assert plan.ok is False
    assert any(refusal.line == 2 and refusal.field == field for refusal in plan.refusals), plan.refusals


def test_the_same_unit_twice_in_one_file_is_refused(kernel):
    plan = VI.plan_import(kernel, file_of(row(), row()))
    assert any("defined twice" in refusal.message for refusal in plan.refusals)


def test_one_property_described_two_ways_in_one_file_is_refused(kernel):
    plan = VI.plan_import(
        kernel,
        file_of(row(), row(unit_id="unit_ubud_02", property_name="Villa Ubud Lain")),
    )
    assert any("described differently" in refusal.message for refusal in plan.refusals)


def test_two_units_of_the_same_property_are_normal(kernel):
    plan = VI.plan_import(kernel, file_of(row(), row(unit_id="unit_ubud_02", unit_name="Unit Dua")))
    assert plan.ok is True
    assert len(plan.rows) == 2
    assert plan.properties_new == 1
    assert plan.units_new == 2


# --------------------------------------------------------------------------- #
# 3. Dry run writes nothing; apply writes once
# --------------------------------------------------------------------------- #
def test_planning_writes_nothing(kernel):
    VI.plan_import(kernel, file_of(row()))
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0
    assert kernel.conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0


def test_a_refused_plan_cannot_be_applied(kernel):
    plan = VI.plan_import(kernel, file_of(row(timezone="Nowhere/Special")))
    with pytest.raises(DomainError) as caught:
        VI.apply_import(kernel, plan, actor="owner_anas", actor_role="owner")
    assert caught.value.code == "IMPORT_PLAN_REFUSED"
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0


def test_a_valid_plan_imports_and_lands_in_the_audit_chain(kernel):
    plan = VI.plan_import(kernel, file_of(row(), row(unit_id="unit_ubud_02")))
    result = VI.apply_import(
        kernel, plan, actor="owner_anas", actor_role="owner", source_ref="villa-list-okt.csv"
    )
    assert (result.properties_created, result.units_created) == (1, 2)
    assert result.audit_events == 3
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
    assert kernel.conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 2
    events = kernel.conn.execute(
        "SELECT action, entity_id, evidence_ref FROM audit_events WHERE action LIKE 'inventory.%'"
    ).fetchall()
    assert {event["entity_id"] for event in events} == {"villa_ubud_01", "unit_ubud_01", "unit_ubud_02"}
    assert all(event["evidence_ref"] == f"import:{plan.fingerprint}" for event in events)


def test_re_running_the_same_file_updates_instead_of_duplicating(kernel):
    text = file_of(row())
    VI.apply_import(kernel, VI.plan_import(kernel, text), actor="owner_anas", actor_role="owner")
    second = VI.plan_import(kernel, text)
    assert second.properties_new == 0 and second.properties_existing == 1
    assert second.units_new == 0 and second.units_existing == 1
    result = VI.apply_import(kernel, second, actor="owner_anas", actor_role="owner")
    assert (result.properties_updated, result.units_updated) == (1, 1)
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
    assert kernel.conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 1


def test_an_operator_may_omit_the_optional_columns(kernel):
    header = "property_id,property_name,region,unit_id,unit_name,bedrooms,bathrooms,base_capacity,max_guests"
    line = "villa_ubud_01,Villa Ubud Satu,Ubud,unit_ubud_01,Unit Utama,2,2,2,4"
    plan = VI.plan_import(kernel, "\n".join([header, line]) + "\n")
    assert plan.ok is True
    VI.apply_import(kernel, plan, actor="gm_ceo", actor_role="gm")
    stored = kernel.conn.execute(
        "SELECT province, timezone, status FROM properties WHERE property_id = 'villa_ubud_01'"
    ).fetchone()
    assert (stored["province"], stored["timezone"], stored["status"]) == (
        "Bali", "Asia/Makassar", "SELLABLE",
    )


# --------------------------------------------------------------------------- #
# 4. Who may import, and where the rows land
# --------------------------------------------------------------------------- #
def test_a_role_without_inventory_authority_may_not_import(kernel):
    plan = VI.plan_import(kernel, file_of(row()))
    for role in ("finance", "auditor", "agent", "ai_service"):
        with pytest.raises(DomainError) as caught:
            VI.apply_import(kernel, plan, actor="someone", actor_role=role)
        assert caught.value.code == "IMPORT_ROLE_REFUSED"
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 0


def test_the_reservation_desk_may_import(kernel):
    plan = VI.plan_import(kernel, file_of(row()))
    result = VI.apply_import(
        kernel, plan, actor="agent_rm_1", actor_role="reservation_manager"
    )
    assert result.units_created == 1


def test_a_row_naming_another_agencys_property_is_refused_whole(kernel):
    """The kernel refuses an id the agency does not own — and nothing half-lands."""
    kernel.conn.execute(
        "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
        " VALUES ('agency_other', 'Other', 'ACTIVE', ?, ?)", (NOW, NOW))
    kernel.conn.execute(
        "INSERT INTO properties (property_id, agency_id, name, property_type, region, province,"
        " timezone, status, contract_pack_status, created_at, updated_at)"
        " VALUES ('villa_ubud_01', 'agency_other', 'Villa Tetangga', 'villa', 'Ubud', 'Bali',"
        " 'Asia/Makassar', 'SELLABLE', 'ACTIVATED', ?, ?)", (NOW, NOW))
    plan = VI.plan_import(kernel, file_of(row(), row(unit_id="unit_ubud_02")))
    # the plan cannot see it as "existing" — it belongs to someone else
    assert plan.properties_existing == 0
    with pytest.raises(DomainError) as caught:
        VI.apply_import(kernel, plan, actor="owner_anas", actor_role="owner")
    assert caught.value.code == "PROPERTY_NOT_FOUND"
    # and the transaction rolled back: no property and no unit exists for this agency yet
    assert kernel.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == 1
    assert kernel.conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0
    assert kernel.conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action LIKE 'inventory.%'"
    ).fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# 5. The shipped template and the DDL cannot drift
# --------------------------------------------------------------------------- #
def test_the_shipped_template_imports_as_it_is(kernel):
    plan = VI.plan_import(kernel, VI.template_csv())
    assert plan.ok is True, plan.refusals
    assert plan.units_new == 1


def _ddl_choices(column: str) -> tuple[str, ...]:
    text = SCHEMA.read_text(encoding="utf-8")
    match = re.search(rf"{column}\s+IN \(([^)]*)\)", text)
    assert match is not None, f"{column} has no CHECK list in the schema"
    values = re.findall(r"'([^']+)'", match.group(1))
    return tuple(sorted(values))


@pytest.mark.parametrize(
    "column,constant",
    [
        ("property_type", VI.PROPERTY_TYPES),
        ("province", VI.PROVINCES),
        ("unit_type", VI.UNIT_TYPES),
    ],
)
def test_the_vocabularies_match_the_schema(column, constant):
    """The DDL is the contract: a copy that drifted would import values the database then refuses."""
    assert tuple(sorted(constant)) == _ddl_choices(column)


def _table_statuses(table: str) -> tuple[str, ...]:
    """The CHECK list on ``status`` inside one table's DDL block (not another table's)."""
    text = SCHEMA.read_text(encoding="utf-8")
    block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", text, re.S)
    assert block is not None, f"no CREATE TABLE for {table}"
    match = re.search(r"status\s+TEXT[^,]*?CHECK \(status IN \(([^)]*)\)\)", block.group(1))
    assert match is not None, f"{table}.status has no CHECK list"
    return tuple(sorted(re.findall(r"'([^']+)'", match.group(1))))


def test_the_status_lists_match_the_schema_and_the_gates():
    """Two homes already exist for these lists (DDL + gates); a third copy must not drift."""
    import gates

    assert set(VI.PROPERTY_STATUSES) == set(_table_statuses("properties"))
    assert set(VI.UNIT_STATUSES) == set(_table_statuses("units"))
    assert set(VI.PROPERTY_STATUSES) == set(gates.PROPERTY_STATUSES)
    assert set(VI.UNIT_STATUSES) == set(gates.UNIT_STATUSES)
    assert set(VI.CONTRACT_PACK_STATUSES) == set(gates.CONTRACT_PACK_STATUSES)


# --------------------------------------------------------------------------- #
# 6. The CLI: dry run by default, and --apply is explicit
# --------------------------------------------------------------------------- #
import subprocess  # noqa: E402
import sys  # noqa: E402
import os  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "import_villas.py"


def run_cli(*args: str, env: dict | None = None):
    merged = dict(os.environ)
    merged.pop("VILLA_DB", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=120, env=merged
    )


def test_the_cli_prints_a_template_that_imports(tmp_path):
    printed = run_cli("--template")
    assert printed.returncode == 0
    plan = VI.plan_import(_kernel_for_cli(), printed.stdout)
    assert plan.ok is True


def test_the_cli_without_villa_db_refuses_to_apply(tmp_path):
    listing = tmp_path / "villas.csv"
    listing.write_text(file_of(row()), encoding="utf-8")
    applied = run_cli("--file", str(listing), "--apply", "--actor", "owner_anas")
    assert applied.returncode == 2
    assert "VILLA_DB" in applied.stderr


def test_the_cli_dry_run_writes_nothing_and_says_so(tmp_path):
    listing = tmp_path / "villas.csv"
    listing.write_text(file_of(row()), encoding="utf-8")
    with _temp_db(tmp_path) as db:
        planned = run_cli("--file", str(listing), env={"VILLA_DB": str(db)})
        assert planned.returncode == 0
        assert "PLAN ONLY" in planned.stdout
        assert _count(db, "properties") == 0


def test_the_cli_apply_writes_and_the_second_run_updates(tmp_path):
    listing = tmp_path / "villas.csv"
    listing.write_text(file_of(row()), encoding="utf-8")
    with _temp_db(tmp_path) as db:
        first = run_cli("--file", str(listing), "--apply", "--actor", "owner_anas",
                        "--source-ref", "villa-list.csv", env={"VILLA_DB": str(db)})
        assert first.returncode == 0, first.stdout
        assert "APPLIED" in first.stdout
        assert _count(db, "properties") == 1
        assert _count(db, "units") == 1
        second = run_cli("--file", str(listing), "--apply", "--actor", "owner_anas",
                         env={"VILLA_DB": str(db)})
        assert second.returncode == 0
        assert _count(db, "properties") == 1, "the second run must not duplicate"


def test_the_cli_refuses_a_file_with_a_bad_row_and_names_the_line(tmp_path):
    listing = tmp_path / "villas.csv"
    listing.write_text(file_of(row(timezone="Nowhere/Special")), encoding="utf-8")
    with _temp_db(tmp_path) as db:
        refused = run_cli("--file", str(listing), env={"VILLA_DB": str(db)})
        assert refused.returncode == 1
        assert "line 2" in refused.stdout
        assert "timezone" in refused.stdout
        assert _count(db, "properties") == 0


def _count(db, table: str) -> int:
    conn = RK.ReservationKernel.open(str(db), clock=FrozenClock.at(NOW),
                                     ids=IdFactory(True, "count"), agency_id=AGENCY)
    try:
        return conn.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _kernel_for_cli():
    return RK.ReservationKernel.open(":memory:", clock=FrozenClock.at(NOW),
                                     ids=IdFactory(True, "cli"), agency_id=AGENCY)


class _temp_db:
    """A real database file: the CLI is a separate process, so :memory: is not enough."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "villa.db"

    def __enter__(self):
        return self.path

    def __exit__(self, *exc) -> None:
        return None


# --------------------------------------------------------------------------- #
# 7. A location needs a source: the four geo columns (ADR-0068)
# --------------------------------------------------------------------------- #
def _property_row(db, property_id: str = "villa_ubud_01"):
    conn = RK.ReservationKernel.open(str(db), clock=FrozenClock.at(NOW),
                                     ids=IdFactory(True, "geo"), agency_id=AGENCY)
    try:
        return conn.conn.execute(
            "SELECT latitude, longitude, geo_precision, geo_source FROM properties"
            " WHERE property_id = ?", (property_id,)
        ).fetchone()
    finally:
        conn.close()


def test_a_verified_row_stores_the_coordinate_and_its_source(tmp_path):
    listing = tmp_path / "villas.csv"
    listing.write_text(
        file_of(row(latitude="-8.5069", longitude="115.2627", geo_precision="VERIFIED",
                    geo_source="survei 2026-11-12 oleh pemilik")),
        encoding="utf-8",
    )
    with _temp_db(tmp_path) as db:
        applied = run_cli("--file", str(listing), "--apply", "--actor", "owner_anas",
                          env={"VILLA_DB": str(db)})
        assert applied.returncode == 0, applied.stdout
        stored = _property_row(db)
        assert stored["geo_precision"] == "VERIFIED"
        assert stored["latitude"] == pytest.approx(-8.5069)
        assert stored["longitude"] == pytest.approx(115.2627)
        assert "survei" in stored["geo_source"]


def test_verified_without_coordinates_is_refused_and_names_the_column(kernel):
    plan = VI.plan_import(kernel, file_of(row(geo_precision="VERIFIED", geo_source="survei")))
    assert plan.ok is False
    assert plan.refusals[0].field == "latitude"
    assert "requires both coordinates" in plan.refusals[0].message


def test_verified_without_a_source_is_refused_and_names_the_column(kernel):
    plan = VI.plan_import(
        kernel, file_of(row(latitude="-8.5", longitude="115.2", geo_precision="VERIFIED"))
    )
    assert plan.ok is False
    assert plan.refusals[0].field == "geo_source"


def test_unknown_may_not_carry_a_coordinate(kernel):
    plan = VI.plan_import(kernel, file_of(row(latitude="-8.5", longitude="115.2")))
    assert plan.ok is False
    assert plan.refusals[0].field == "geo_precision"


def test_a_coordinate_outside_the_valid_range_is_refused(kernel):
    plan = VI.plan_import(
        kernel, file_of(row(latitude="95", longitude="115.2", geo_precision="AREA_APPROX",
                            geo_source="peta desa"))
    )
    assert plan.ok is False
    assert plan.refusals[0].field == "latitude"


def test_a_later_file_without_geo_columns_does_not_erase_a_survey(kernel):
    """Re-importing the list written before the survey must not un-survey the villa."""
    first = file_of(row(latitude="-8.5069", longitude="115.2627", geo_precision="VERIFIED",
                        geo_source="survei 2026-11-12"))
    assert VI.apply_import(kernel, VI.plan_import(kernel, first), actor="owner_anas",
                           actor_role="owner").properties_created == 1
    before = kernel.conn.execute(
        "SELECT geo_precision, geo_source FROM properties WHERE property_id = 'villa_ubud_01'"
    ).fetchone()
    assert before["geo_precision"] == "VERIFIED"

    second = file_of(row())  # the same villa list, geo columns left blank
    VI.apply_import(kernel, VI.plan_import(kernel, second), actor="owner_anas",
                    actor_role="owner")
    after = kernel.conn.execute(
        "SELECT geo_precision, latitude, geo_source FROM properties"
        " WHERE property_id = 'villa_ubud_01'"
    ).fetchone()
    assert after["geo_precision"] == "VERIFIED"
    assert after["latitude"] == pytest.approx(-8.5069)


def test_the_template_carries_the_geo_columns_in_schema_order():
    header = VI.template_csv().splitlines()[0].split(",")
    assert header == list(VI.COLUMNS)
    assert header[8:12] == ["latitude", "longitude", "geo_precision", "geo_source"]
