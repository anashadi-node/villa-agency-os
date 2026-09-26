"""Option sets: the villas an agency offers for one inquiry (§5, §34).

Master Spec v1.1 §5 puts option sets between the inquiry and the villas:

    INQUIRY -> OPTION SETS -> VILLA -> VILLA_SUPPLIER_RELATIONSHIP -> CONTRACT

§34 asks for "Explainable Villa Matching (no opaque magic score)" and a "Villa
Compare + Option Set Builder", so an option set stores the order a human chose and
this module never invents a score of its own.

Append-only, like the rest of the ledger: a change is a new version, and presenting
a set to a guest is a row of its own. That is what makes "what the guest was shown"
reproducible afterwards (§6 preserve context, §27 evidence discipline).

Presenting is a client communication, which §56 classes as medium risk and reserves
for policy-controlled tools. No policy engine exists yet, so `present` fails closed
for machine identities instead of pretending to gate them. Building a set from a
deterministic search is low risk (§56) and stays open to any actor — recorded, not
inferred.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    require_ref,
    require_text,
    to_iso,
)
from supplier_route import is_machine_actor

__all__ = [
    "PRESENTATION_CHANNELS",
    "AlreadyPresented",
    "InvalidOptionSet",
    "MachineActorRefused",
    "OptionSet",
    "OptionSetError",
    "OptionSetItem",
    "OptionSetNotFound",
    "OptionSetService",
    "Presentation",
    "OptionSetInquiryNotFound",
]

#: Mirrors the conversation channels the platform already records, so an option set
#: introduces no new vocabulary for "how the guest was told".
PRESENTATION_CHANNELS: tuple[str, ...] = (
    "website",
    "email",
    "whatsapp_business",
    "dashboard",
    "phone",
)


class OptionSetError(DomainError):
    """A deliberate refusal from the option set domain."""


class OptionSetInquiryNotFound(OptionSetError):
    """No such inquiry inside this agency."""


class OptionSetNotFound(OptionSetError):
    """No such option set inside this agency."""


class InvalidOptionSet(OptionSetError):
    """The requested set is not one this platform may store."""


class AlreadyPresented(OptionSetError):
    """This version was already shown to the guest; a change is a new version."""


class MachineActorRefused(OptionSetError):
    """Presentation is human authority (§56): a machine identity is refused."""


@dataclass(frozen=True)
class OptionSetItem:
    """One villa offered inside a set, in the agency's own order."""

    option_set_item_id: str
    property_id: str
    position: int
    unit_id: Optional[str] = None
    source_match_run_id: Optional[str] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class OptionSet:
    """A versioned selection of villas for one inquiry."""

    option_set_id: str
    inquiry_id: str
    version: int
    label: Optional[str]
    created_by: str
    created_at: str
    items: tuple[OptionSetItem, ...]


@dataclass(frozen=True)
class Presentation:
    """The moment a version was shown to the guest."""

    presentation_id: str
    option_set_id: str
    presented_by: str
    presented_at: str
    channel: str


class OptionSetService:
    """Reads and writes option sets for one agency."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Any = None,
        ids: Any = None,
        agency_id: str = DEFAULT_AGENCY_ID,
    ) -> None:
        self.conn = conn
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id", max_len=64)

    # -- writes ------------------------------------------------------------------

    def build(
        self,
        inquiry_id: Any,
        items: Sequence[Any],
        *,
        actor: Any,
        label: Any = None,
    ) -> OptionSet:
        """Create the next version of the option set for this inquiry.

        A second call never edits the first version: the platform keeps what the
        guest was shown, so a changed offer is a new version.
        """
        case_id = self._require_inquiry(inquiry_id)
        who = require_ref(actor, "actor", max_len=64)
        normalised = self._normalise_items(case_id, items)
        clean_label = None if label is None else require_text(label, "label", max_len=120)
        now = to_iso(self.clock.now())

        self.conn.execute("SAVEPOINT build_option_set")
        try:
            version = self._next_version(case_id)
            option_set_id = self.ids.new("option_set")
            self.conn.execute(
                "INSERT INTO option_sets (option_set_id, case_id, version, label, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (option_set_id, case_id, version, clean_label, who, now),
            )
            for position, item in enumerate(normalised, start=1):
                self.conn.execute(
                    "INSERT INTO option_set_items (option_set_item_id, option_set_id, position,"
                    " property_id, unit_id, source_match_run_id, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.ids.new("option_set_item"),
                        option_set_id,
                        position,
                        item["property_id"],
                        item.get("unit_id"),
                        item.get("source_match_run_id"),
                        item.get("note"),
                        now,
                    ),
                )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT build_option_set")
            self.conn.execute("RELEASE SAVEPOINT build_option_set")
            raise
        self.conn.execute("RELEASE SAVEPOINT build_option_set")
        return self.option_set(option_set_id)

    def revise(
        self,
        option_set_id: Any,
        items: Sequence[Any],
        *,
        actor: Any,
        label: Any = None,
    ) -> OptionSet:
        """Build a new version of an existing set, keeping the earlier one intact."""
        current = self.option_set(option_set_id)
        chosen_label = current.label if label is None else label
        return self.build(current.inquiry_id, items, actor=actor, label=chosen_label)

    def present(self, option_set_id: Any, *, actor: Any, actor_role: Any, channel: Any) -> Presentation:
        """Record that this version was shown to the guest.

        Human authority: §56 keeps client communication behind policy-controlled
        tools, and no policy engine exists yet, so a machine identity is refused
        rather than waved through.
        """
        target = require_ref(option_set_id, "option_set_id", max_len=64)
        who = require_ref(actor, "actor", max_len=64)
        role = require_ref(actor_role, "actor_role", max_len=64)
        if is_machine_actor(who, role):
            raise MachineActorRefused(
                "presenting an option set is a client communication: human authority (§56)",
                code="MACHINE_ACTOR_REFUSED",
                actor=who,
                actor_role=role,
            )
        if channel not in PRESENTATION_CHANNELS:
            raise InvalidOptionSet(
                f"channel is not one this platform records: {channel!r}",
                code="INVALID_CHANNEL",
                channel=channel,
                known=tuple(PRESENTATION_CHANNELS),
            )
        self.option_set(target)  # inside this agency, or it does not exist
        now = to_iso(self.clock.now())

        self.conn.execute("SAVEPOINT present_option_set")
        try:
            row = self.conn.execute(
                "SELECT presentation_id FROM option_set_presentations WHERE option_set_id = ?",
                (target,),
            ).fetchone()
            if row is not None:
                raise AlreadyPresented(
                    "this version was already presented: change it into a new version instead",
                    code="ALREADY_PRESENTED",
                    option_set_id=target,
                )
            presentation_id = self.ids.new("presentation")
            self.conn.execute(
                "INSERT INTO option_set_presentations (presentation_id, option_set_id, presented_by,"
                " presented_at, channel) VALUES (?, ?, ?, ?, ?)",
                (presentation_id, target, who, now, channel),
            )
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT present_option_set")
            self.conn.execute("RELEASE SAVEPOINT present_option_set")
            raise
        self.conn.execute("RELEASE SAVEPOINT present_option_set")
        return Presentation(presentation_id, target, who, now, channel)

    # -- reads -------------------------------------------------------------------

    def option_set(self, option_set_id: Any) -> OptionSet:
        """One version, with its items in the order the agency chose."""
        target = require_ref(option_set_id, "option_set_id", max_len=64)
        row = self.conn.execute(
            "SELECT s.option_set_id, s.case_id, s.version, s.label, s.created_by, s.created_at"
            " FROM option_sets s JOIN cases c ON c.case_id = s.case_id"
            " WHERE s.option_set_id = ? AND c.agency_id = ?",
            (target, self.agency_id),
        ).fetchone()
        if row is None:
            raise OptionSetNotFound(
                f"option set not found in this agency: {target}", code="OPTION_SET_NOT_FOUND"
            )
        return self._hydrate(row)

    def current(self, inquiry_id: Any) -> Optional[OptionSet]:
        """The newest version for this inquiry, or None when none was built yet."""
        case_id = self._require_inquiry(inquiry_id)
        row = self.conn.execute(
            "SELECT option_set_id FROM option_sets WHERE case_id = ? ORDER BY version DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        return None if row is None else self.option_set(row["option_set_id"])

    def history(self, inquiry_id: Any) -> tuple[OptionSet, ...]:
        """Every version, oldest first — the offer's own audit trail."""
        case_id = self._require_inquiry(inquiry_id)
        rows = self.conn.execute(
            "SELECT option_set_id FROM option_sets WHERE case_id = ? ORDER BY version ASC",
            (case_id,),
        ).fetchall()
        return tuple(self.option_set(row["option_set_id"]) for row in rows)

    def presentation(self, option_set_id: Any) -> Optional[Presentation]:
        """When this version was shown, or None while it is still internal."""
        target = self.option_set(option_set_id).option_set_id
        row = self.conn.execute(
            "SELECT presentation_id, option_set_id, presented_by, presented_at, channel"
            " FROM option_set_presentations WHERE option_set_id = ?",
            (target,),
        ).fetchone()
        if row is None:
            return None
        return Presentation(
            row["presentation_id"],
            row["option_set_id"],
            row["presented_by"],
            row["presented_at"],
            row["channel"],
        )

    # -- internals ---------------------------------------------------------------

    def _hydrate(self, row: sqlite3.Row) -> OptionSet:
        item_rows = self.conn.execute(
            "SELECT option_set_item_id, property_id, position, unit_id, source_match_run_id, note"
            " FROM option_set_items WHERE option_set_id = ? ORDER BY position ASC",
            (row["option_set_id"],),
        ).fetchall()
        items = tuple(
            OptionSetItem(
                item["option_set_item_id"],
                item["property_id"],
                item["position"],
                item["unit_id"],
                item["source_match_run_id"],
                item["note"],
            )
            for item in item_rows
        )
        return OptionSet(
            row["option_set_id"],
            row["case_id"],
            row["version"],
            row["label"],
            row["created_by"],
            row["created_at"],
            items,
        )

    def _require_inquiry(self, inquiry_id: Any) -> str:
        case_id = require_ref(inquiry_id, "inquiry_id", max_len=64)
        row = self.conn.execute(
            "SELECT 1 FROM cases WHERE case_id = ? AND agency_id = ?",
            (case_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise OptionSetInquiryNotFound(
                f"inquiry not found in this agency: {case_id}",
                code="OPTION_SET_INQUIRY_NOT_FOUND",
                inquiry_id=case_id,
            )
        return case_id

    def _next_version(self, case_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS highest FROM option_sets WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        return int(row["highest"]) + 1

    def _normalise_items(self, case_id: str, items: Sequence[Any]) -> tuple[Mapping[str, Any], ...]:
        """Refuse anything that would offer a villa the agency does not own.

        Provenance to a match run is optional on purpose: a human may offer a villa
        the ranking never produced (a known favourite, a direct request), and §34
        forbids an opaque score — not a human's judgement.
        """
        if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
            raise InvalidOptionSet(
                "items must be a sequence of villas", code="INVALID_ITEMS", got=type(items).__name__
            )
        if not items:
            raise InvalidOptionSet(
                "an option set with no villas is not an offer", code="EMPTY_OPTION_SET"
            )

        normalised: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for raw in items:
            if isinstance(raw, str):
                raw = {"property_id": raw}
            if not isinstance(raw, Mapping):
                raise InvalidOptionSet(
                    "each item is a villa reference or a mapping", code="INVALID_ITEM", item=raw
                )
            property_id = require_ref(raw.get("property_id"), "property_id", max_len=64)
            if property_id in seen:
                raise InvalidOptionSet(
                    f"villa appears twice in one set: {property_id}",
                    code="DUPLICATE_VILLA",
                    property_id=property_id,
                )
            seen.add(property_id)
            self._require_owned_property(property_id)

            unit_id = raw.get("unit_id")
            if unit_id is not None:
                unit_id = require_ref(unit_id, "unit_id", max_len=64)
                self._require_unit_of_property(unit_id, property_id)

            run_id = raw.get("source_match_run_id")
            if run_id is not None:
                run_id = require_ref(run_id, "source_match_run_id", max_len=64)
                self._require_run_of_inquiry(run_id, case_id)

            note = raw.get("note")
            if note is not None:
                note = require_text(note, "note", max_len=500)

            normalised.append(
                {
                    "property_id": property_id,
                    "unit_id": unit_id,
                    "source_match_run_id": run_id,
                    "note": note,
                }
            )
        return tuple(normalised)

    def _require_owned_property(self, property_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM properties WHERE property_id = ? AND agency_id = ?",
            (property_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise InvalidOptionSet(
                f"villa not found in this agency: {property_id}",
                code="VILLA_NOT_FOUND",
                property_id=property_id,
            )

    def _require_unit_of_property(self, unit_id: str, property_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM units WHERE unit_id = ? AND property_id = ? AND agency_id = ?",
            (unit_id, property_id, self.agency_id),
        ).fetchone()
        if row is None:
            raise InvalidOptionSet(
                f"unit does not belong to this villa in this agency: {unit_id}",
                code="UNIT_NOT_IN_VILLA",
                unit_id=unit_id,
                property_id=property_id,
            )

    def _require_run_of_inquiry(self, run_id: str, case_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM match_runs WHERE match_run_id = ? AND case_id = ?",
            (run_id, case_id),
        ).fetchone()
        if row is None:
            raise InvalidOptionSet(
                f"match run does not belong to this inquiry: {run_id}",
                code="RUN_NOT_IN_INQUIRY",
                source_match_run_id=run_id,
                inquiry_id=case_id,
            )
