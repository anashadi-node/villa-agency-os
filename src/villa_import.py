"""Bulk inventory import: a tenant's villa list arrives as one file, not two hundred forms.

Why a file and not a form
-------------------------
An agency that signs up brings a list: twenty villas, forty units, already written down in a
spreadsheet. Asking a human to retype it is both slow and a way to introduce the exact errors the
platform then refuses later (a typo in a timezone, a capacity that contradicts itself). This
module reads that file once, refuses what is wrong **by line number**, and writes what is right.

Five rules, each one a way this goes wrong otherwise:

* **Dry run first.** :func:`plan` validates and reports; :func:`apply` writes. A file that is not
  valid is never partially applied: one bad line refuses the whole import, and the report names
  the line. A half-imported villa list is worse than none — nobody can tell what landed.
* **The tenant cannot name the tenant.** ``agency_id`` comes from the caller's context, never from
  the file. A column in a CSV must never be able to write into another agency's inventory.
* **Upsert, not insert.** The writers are the kernel's own ``upsert_property``/``upsert_unit``, so
  a re-run of the same file updates instead of duplicating, and the platform keeps one writer per
  table.
* **The file shape is checked before the values.** An unknown column is refused, because a column
  nobody reads is a promise to the operator that their data landed. A missing required column names
  itself, rather than every row failing mysteriously.
* **A location needs a source.** Four optional columns carry where a villa is; the rule lives in
  :func:`geo.validate_geo_fields`, so an import cannot mark a villa ``VERIFIED`` without both
  coordinates and the sentence that says who confirmed them (ADR-0068). A file may leave the
  columns out — it may not half-fill them, and it can never erase a survey already recorded:
  ``geo_precision`` is only written when the row names one.
* **Every row is in the audit chain.** One event per property and per unit, carrying the file's
  fingerprint, so "which file put this villa here" is answerable years later.

CSV only, deliberately: the standard library reads it, Excel exports it, and a new dependency for
an occasional import would be the only reason to run code nobody else runs.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import geo
from audit import append_event
from primitives import DomainError, IdFactory, require_ref

__all__ = [
    "IMPORT_ROLES",
    "ImportResult",
    "ImportRow",
    "InvalidImportFile",
    "PROPERTY_COLUMNS",
    "PROPERTY_STATUSES",
    "PROPERTY_TYPES",
    "PROVINCES",
    "REQUIRED_COLUMNS",
    "RowRefusal",
    "UNIT_COLUMNS",
    "UNIT_STATUSES",
    "UNIT_TYPES",
    "VillaImportPlan",
    "apply_import",
    "plan_import",
    "read_rows",
    "template_csv",
]

#: Who may import inventory. Inventory is operational data: the tenant's own owner/GM, its system
#: admin, and the reservation/operations desk that maintains the list. The AI is not here, and a
#: finance or auditor role is not either: neither owns the villa list.
IMPORT_ROLES: tuple[str, ...] = ("owner", "gm", "system_admin", "reservation_manager")

#: Enumerations taken from `src/schema/001_initial.sql`. `tests/test_villa_import.py` compares
#: them to the DDL's own CHECK lists, so a schema change cannot silently leave a stale copy here.
PROPERTY_TYPES: tuple[str, ...] = ("villa", "resort", "guesthouse", "apartment", "homestay")
PROVINCES: tuple[str, ...] = ("Bali", "DI Yogyakarta")
PROPERTY_STATUSES: tuple[str, ...] = ("SELLABLE", "SELLABLE_WITH_REVIEW", "NOT_SELLABLE", "SUSPENDED")
CONTRACT_PACK_STATUSES: tuple[str, ...] = ("DRAFT", "VERIFIED", "ACTIVATED", "EXPIRED")
UNIT_TYPES: tuple[str, ...] = ("entire_villa", "suite", "room", "bungalow", "compound_unit")
UNIT_STATUSES: tuple[str, ...] = ("ACTIVE", "MAINTENANCE", "SUSPENDED")

#: One row describes one **unit**, repeating its property's columns. That is how a villa list is
#: actually written down, and it keeps a property with no units from being a special case nobody
#: thinks about until the first booking.
PROPERTY_COLUMNS: tuple[str, ...] = (
    "property_id",
    "property_name",
    "property_type",
    "region",
    "province",
    "timezone",
    "property_status",
    "contract_pack_status",
    "latitude",
    "longitude",
    "geo_precision",
    "geo_source",
)
UNIT_COLUMNS: tuple[str, ...] = (
    "unit_id",
    "unit_name",
    "unit_type",
    "bedrooms",
    "bathrooms",
    "base_capacity",
    "max_guests",
    "unit_status",
)
COLUMNS: tuple[str, ...] = PROPERTY_COLUMNS + UNIT_COLUMNS

#: Columns a row cannot do without. Everything else falls back to the row's default below, so an
#: operator may leave a column out entirely and still be understood.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "property_id",
    "property_name",
    "region",
    "unit_id",
    "unit_name",
    "bedrooms",
    "bathrooms",
    "base_capacity",
    "max_guests",
)

#: Defaults for the columns an operator may omit: what the platform assumes when the list says
#: nothing. Each one is a *choice the tenant would have made anyway*, not a hidden policy.
COLUMN_DEFAULTS: Mapping[str, str] = MappingProxyType(
    {
        "property_type": "villa",
        "province": "Bali",
        "timezone": "Asia/Makassar",
        "property_status": "SELLABLE",
        "contract_pack_status": "VERIFIED",
        "unit_type": "entire_villa",
        "unit_status": "ACTIVE",
    }
)

MAX_NAME_CHARS = 120  # the kernel's own limit for a unit name; property names allow 200
MAX_FILE_BYTES = 2_000_000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class InvalidImportFile(DomainError):
    """The file itself cannot be read as an inventory list."""

    code = "INVALID_IMPORT_FILE"


@dataclass(frozen=True)
class RowRefusal:
    line: int
    field: str
    message: str
    value: str

    def as_text(self) -> str:
        return f"line {self.line}: {self.field} — {self.message} ({self.value!r})"


@dataclass(frozen=True)
class ImportRow:
    line: int
    property_id: str
    unit_id: str
    property_values: Mapping[str, Any]
    unit_values: Mapping[str, Any]


@dataclass(frozen=True)
class VillaImportPlan:
    """What the file would do, before it does it."""

    rows: tuple[ImportRow, ...]
    refusals: tuple[RowRefusal, ...]
    properties_new: int
    properties_existing: int
    units_new: int
    units_existing: int
    fingerprint: str

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def summary(self) -> str:
        return (
            f"{len(self.rows)} row(s): {self.properties_new} new property(ies), "
            f"{self.properties_existing} updated, {self.units_new} new unit(s), "
            f"{self.units_existing} updated, {len(self.refusals)} refused"
        )


@dataclass(frozen=True)
class ImportResult:
    fingerprint: str
    properties_created: int
    properties_updated: int
    units_created: int
    units_updated: int
    audit_events: int

    @property
    def summary(self) -> str:
        return (
            f"imported {self.properties_created} new property(ies), "
            f"{self.properties_updated} updated, {self.units_created} new unit(s), "
            f"{self.units_updated} updated, {self.audit_events} audit event(s)"
        )


def _fingerprint(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def read_rows(text: str) -> tuple[dict[str, str], ...]:
    """Parse the file, refusing a shape the importer cannot honour.

    The header is checked *first*: an unknown column, a duplicate column, or a missing required one
    is a refusal that names the column, rather than two hundred rows failing one at a time.
    """
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise InvalidImportFile(
            "this file is larger than the importer accepts",
            bytes=len(text.encode("utf-8")),
            limit=MAX_FILE_BYTES,
        )
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames
    if not header:
        raise InvalidImportFile("the file has no header row")
    cleaned = [column.strip() for column in header]
    if len(set(cleaned)) != len(cleaned):
        raise InvalidImportFile("the header repeats a column name", header=cleaned)
    unknown = [column for column in cleaned if column not in COLUMNS]
    if unknown:
        raise InvalidImportFile(
            "the header names columns this importer does not read",
            columns=unknown,
            known=list(COLUMNS),
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in cleaned]
    if missing:
        raise InvalidImportFile(
            "the header is missing required columns", columns=missing, required=list(REQUIRED_COLUMNS)
        )
    rows: list[dict[str, str]] = []
    for raw in reader:
        row = {
            (key or "").strip(): (value or "").strip()
            for key, value in raw.items()
            if key is not None
        }
        if not any(row.values()):
            continue  # a blank line at the end of an exported sheet is not a row
        rows.append(row)
    if not rows:
        raise InvalidImportFile("the file has a header but no rows")
    return tuple(rows)


def _required(row: Mapping[str, str], line: int, field: str, refusals: list[RowRefusal]) -> str:
    value = str(row.get(field, "")).strip()
    if not value:
        refusals.append(RowRefusal(line, field, "empty, and this column is required", value))
    return value


def _choice(
    row: Mapping[str, str], line: int, field: str, allowed: Sequence[str], refusals: list[RowRefusal]
) -> str:
    value = str(row.get(field, "")).strip() or COLUMN_DEFAULTS.get(field, "")
    if value not in allowed:
        refusals.append(
            RowRefusal(line, field, f"must be one of {list(allowed)}", value)
        )
    return value


def _whole_number(
    row: Mapping[str, str], line: int, field: str, *, minimum: int, refusals: list[RowRefusal]
) -> int:
    raw = str(row.get(field, "")).strip()
    try:
        number = int(raw)
    except ValueError:
        refusals.append(RowRefusal(line, field, "must be a whole number", raw))
        return 0
    if number < minimum:
        refusals.append(RowRefusal(line, field, f"must be at least {minimum}", raw))
        return 0
    return number


def _identifier(
    row: Mapping[str, str], line: int, field: str, refusals: list[RowRefusal]
) -> str:
    value = _required(row, line, field, refusals)
    if value and not _ID_RE.match(value):
        refusals.append(
            RowRefusal(
                line,
                field,
                "must be lowercase letters, digits, dash or underscore (3 to 64 characters)",
                value,
            )
        )
    return value


def _geo(
    row: Mapping[str, str], line: int, refusals: list[RowRefusal]
) -> tuple[str, Optional[float], Optional[float], Optional[str]]:
    """The four location columns, through the single rule in :func:`geo.validate_geo_fields`."""
    try:
        return geo.validate_geo_fields(
            row.get("geo_precision", ""),
            row.get("latitude", ""),
            row.get("longitude", ""),
            row.get("geo_source", ""),
        )
    except geo.GeoError as exc:
        field = str(exc.details.get("field") or "geo_precision")
        refusals.append(RowRefusal(line, field, str(exc), str(exc.details.get("value") or "")))
        return "UNKNOWN", None, None, None


def plan_import(kernel: Any, text: str) -> VillaImportPlan:
    """Validate every row and classify it as new or existing. Writes nothing."""
    rows = read_rows(text)
    refusals: list[RowRefusal] = []
    planned: list[ImportRow] = []
    seen_units: dict[str, int] = {}
    seen_properties: dict[str, tuple[Any, ...]] = {}

    for offset, row in enumerate(rows, start=2):  # line 1 is the header
        line = offset
        property_id = _identifier(row, line, "property_id", refusals)
        unit_id = _identifier(row, line, "unit_id", refusals)
        property_name = _required(row, line, "property_name", refusals)
        unit_name = _required(row, line, "unit_name", refusals)
        region = _required(row, line, "region", refusals)
        if len(property_name) > 200:
            refusals.append(RowRefusal(line, "property_name", "longer than 200 characters", property_name))
        if len(unit_name) > MAX_NAME_CHARS:
            refusals.append(
                RowRefusal(line, "unit_name", f"longer than {MAX_NAME_CHARS} characters", unit_name)
            )
        if region and len(region) < 2:
            refusals.append(RowRefusal(line, "region", "must be at least 2 characters", region))

        property_type = _choice(row, line, "property_type", PROPERTY_TYPES, refusals)
        province = _choice(row, line, "province", PROVINCES, refusals)
        property_status = _choice(row, line, "property_status", PROPERTY_STATUSES, refusals)
        contract_pack_status = _choice(
            row, line, "contract_pack_status", CONTRACT_PACK_STATUSES, refusals
        )
        unit_type = _choice(row, line, "unit_type", UNIT_TYPES, refusals)
        unit_status = _choice(row, line, "unit_status", UNIT_STATUSES, refusals)
        geo_precision, latitude, longitude, geo_source = _geo(row, line, refusals)

        timezone = str(row.get("timezone", "")).strip() or COLUMN_DEFAULTS["timezone"]
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            refusals.append(
                RowRefusal(line, "timezone", "is not a known timezone name (e.g. Asia/Makassar)", timezone)
            )

        bedrooms = _whole_number(row, line, "bedrooms", minimum=0, refusals=refusals)
        bathrooms = _whole_number(row, line, "bathrooms", minimum=0, refusals=refusals)
        base_capacity = _whole_number(row, line, "base_capacity", minimum=1, refusals=refusals)
        max_guests = _whole_number(row, line, "max_guests", minimum=1, refusals=refusals)
        if base_capacity and max_guests and max_guests < base_capacity:
            refusals.append(
                RowRefusal(line, "max_guests", "must be at least base_capacity", str(max_guests))
            )

        if unit_id:
            if unit_id in seen_units:
                refusals.append(
                    RowRefusal(line, "unit_id", f"defined twice in this file (also line {seen_units[unit_id]})", unit_id)
                )
            else:
                seen_units[unit_id] = line
        if property_id:
            signature = (property_name, property_type, region, province, timezone, property_status,
                         contract_pack_status, geo_precision, latitude, longitude, geo_source)
            previous = seen_properties.get(property_id)
            if previous is None:
                seen_properties[property_id] = signature
            elif previous != signature:
                refusals.append(
                    RowRefusal(
                        line,
                        "property_id",
                        "this property is described differently on an earlier line",
                        property_id,
                    )
                )

        property_values: dict[str, Any] = {
            "property_id": property_id,
            "name": property_name,
            "property_type": property_type,
            "region": region,
            "province": province,
            "timezone": timezone,
            "status": property_status,
            "contract_pack_status": contract_pack_status,
        }
        if geo_precision != "UNKNOWN":
            # Named precision only. An omitted column leaves the four geo columns untouched, so
            # re-importing a list written before the survey cannot un-survey a villa.
            property_values.update(
                geo_precision=geo_precision,
                latitude=latitude,
                longitude=longitude,
                geo_source=geo_source,
            )
        planned.append(
            ImportRow(
                line=line,
                property_id=property_id,
                unit_id=unit_id,
                property_values=property_values,
                unit_values={
                    "unit_id": unit_id,
                    "property_id": property_id,
                    "name": unit_name,
                    "unit_type": unit_type,
                    "bedrooms": bedrooms,
                    "bathrooms": bathrooms,
                    "base_capacity": base_capacity,
                    "max_guests": max_guests,
                    "status": unit_status,
                },
            )
        )

    agency_id = kernel.agency_id
    property_ids = {row.property_id for row in planned if row.property_id}
    unit_ids = {row.unit_id for row in planned if row.unit_id}
    existing_properties = _existing(kernel.conn, "properties", "property_id", property_ids, agency_id)
    existing_units = _existing(kernel.conn, "units", "unit_id", unit_ids, agency_id)

    return VillaImportPlan(
        rows=tuple(planned),
        refusals=tuple(refusals),
        properties_new=len(property_ids - existing_properties),
        properties_existing=len(property_ids & existing_properties),
        units_new=len(unit_ids - existing_units),
        units_existing=len(unit_ids & existing_units),
        fingerprint=_fingerprint(text),
    )


def _existing(
    conn: Any, table: str, column: str, ids: Iterable[str], agency_id: str
) -> set[str]:
    """Which of these ids already exist **for this agency**.

    A row naming another agency's id is not "existing" here: the kernel will refuse it with
    PROPERTY_NOT_FOUND/UNIT_NOT_FOUND, and the import reports that rather than silently updating a
    row the tenant does not own.
    """
    found: set[str] = set()
    for identifier in ids:
        row = conn.execute(
            f"SELECT agency_id FROM {table} WHERE {column} = ?", (identifier,)  # noqa: S608
        ).fetchone()
        if row is not None and _agency_of(row) == agency_id:
            found.add(identifier)
    return found


def _agency_of(row: Any) -> str:
    try:
        return str(row["agency_id"])
    except (TypeError, IndexError, KeyError):  # pragma: no cover - driver shape differences
        return str(row[0])


def _audit_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """An audit payload carries no floats: a coordinate travels as its decimal string.

    ``audit`` refuses floats on purpose — evidence is read by humans and compared by machines, and
    a binary fraction is neither. The column keeps the float SQLite stores; the event keeps the
    number the operator typed.
    """
    payload = dict(values)
    for key in ("latitude", "longitude"):
        if payload.get(key) is not None:
            payload[key] = str(payload[key])
    return payload


def apply_import(
    kernel: Any,
    plan: VillaImportPlan,
    *,
    actor: str,
    actor_role: str,
    source_ref: Optional[str] = None,
    ids: Optional[IdFactory] = None,
) -> ImportResult:
    """Write a valid plan. Refused rows stop the whole import: a half-imported list is worse."""
    actor = require_ref(actor, "actor", max_len=128)
    role = require_ref(actor_role, "actor_role", max_len=64)
    if role not in IMPORT_ROLES:
        raise DomainError(
            "this role may not import inventory",
            code="IMPORT_ROLE_REFUSED",
            role=role,
            allowed=list(IMPORT_ROLES),
        )
    if not plan.ok:
        raise DomainError(
            "refusing to import a file with refused rows: fix them and plan again",
            code="IMPORT_PLAN_REFUSED",
            refusals=[refusal.as_text() for refusal in plan.refusals][:20],
        )

    factory = ids if ids is not None else IdFactory()
    reason = f"bulk inventory import ({source_ref or 'no source named'})"
    properties_created = properties_updated = units_created = units_updated = 0
    audit_events = 0
    existing_properties = _existing(kernel.conn, "properties", "property_id",
                                    {row.property_id for row in plan.rows}, kernel.agency_id)
    existing_units = _existing(kernel.conn, "units", "unit_id",
                              {row.unit_id for row in plan.rows}, kernel.agency_id)

    written_properties: set[str] = set()
    with kernel.transaction() as conn:
        for row in plan.rows:
            if row.property_id not in written_properties:
                kernel.upsert_property(join=True, **row.property_values)
                written_properties.add(row.property_id)
                if row.property_id in existing_properties:
                    properties_updated += 1
                else:
                    properties_created += 1
                append_event(
                    conn,
                    clock=kernel.clock,
                    ids=factory,
                    action="inventory.property_imported",
                    entity_type="property",
                    entity_id=row.property_id,
                    actor=actor,
                    actor_role=role,
                    before=None,
                    after=_audit_payload(row.property_values),
                    reason=f"{reason}; file {plan.fingerprint[:12]}",
                    evidence_ref=f"import:{plan.fingerprint}",
                )
                audit_events += 1
            kernel.upsert_unit(join=True, **row.unit_values)
            if row.unit_id in existing_units:
                units_updated += 1
            else:
                units_created += 1
            append_event(
                conn,
                clock=kernel.clock,
                ids=factory,
                action="inventory.unit_imported",
                entity_type="unit",
                entity_id=row.unit_id,
                actor=actor,
                actor_role=role,
                before=None,
                after=dict(row.unit_values),
                reason=f"{reason}; file {plan.fingerprint[:12]}",
                evidence_ref=f"import:{plan.fingerprint}",
            )
            audit_events += 1

    return ImportResult(
        fingerprint=plan.fingerprint,
        properties_created=properties_created,
        properties_updated=properties_updated,
        units_created=units_created,
        units_updated=units_updated,
        audit_events=audit_events,
    )


def template_csv() -> str:
    """The header an operator should start from, plus one example row.

    Shipped by the CLI (`--template`) so nobody has to guess a column name: the file shape is the
    importer's contract, and a guessed header is the most common way this fails.
    """
    header = ",".join(COLUMNS)
    example = (
        "villa_ubud_01,Villa Ubud Satu,villa,Ubud,Bali,Asia/Makassar,SELLABLE,ACTIVATED,"
        ",,UNKNOWN,,"
        "unit_ubud_01,Unit Utama,entire_villa,2,2,2,4,ACTIVE"
    )
    return f"{header}\n{example}\n"
