"""WAVE 4-C tests: quotation versions, their snapshots, and the acceptance (§11).

§11 asks for three things that are easy to fake and hard to actually do:

1. **Immutable versioning.** A quotation is V1, V2, V3 …, and a version that was sent
   or accepted is never overwritten. The tests below pin the guarantee at the
   *database*, not at the module: a direct UPDATE of a sent version's content has to
   fail even when nothing in ``quotation.py`` is involved.
2. **An acceptance that names what the guest agreed to.** ``quotation_version_id``,
   ``price_snapshot_id``, ``terms_snapshot_id``, ``policy_snapshot_id``,
   ``accepted_at`` — five references, because a price that can be re-derived later is
   not the price that was accepted.
3. **A §56 gate on sending.** Sending is a client communication: medium risk,
   policy-controlled, deny-first until the owner publishes ``quotation.send`` on the
   allowance (ADR-0023).
"""

from __future__ import annotations

import json
import re
import sqlite3
from itertools import count

import pytest

import ai_service as AIS
import policy_engine as PE
import quote_calculator as QC
import quotation as QT
import reservation_kernel as RK
from primitives import DomainError, FrozenClock, IdFactory

NOW = "2026-09-21T00:00:00Z"
#: ``to_iso`` normalises a ``Z`` suffix to ``+00:00``; compare against what is stored.
NOW_ISO = "2026-09-21T00:00:00+00:00"
EFFECTIVE_FROM = "2026-09-21"
AGENCY = "agency_default"
OTHER_AGENCY = "agency_other"
HUMAN = "user:owner-1"
HUMAN_ROLE = "owner"
MACHINE = "ai:reservation_agent"
MACHINE_ROLE = "ai_service"
ALLOWANCE = "security.ai.medium_tool_allowance"
OTHER_TOOL = "availability.request"
CHECK_IN = "2026-10-01"
CHECK_OUT = "2026-10-03"
SOURCE = "§56 AI Reservation Agent Autonomy (LOCKED) — medium-risk policy-controlled tools"

#: Every service instance gets its own deterministic id sequence (see ``svc``).
_SEQUENCES = count(1)


# --------------------------------------------------------------------------- #
# Fixtures: one property, one unit, one policy, one rate card, one quote
# --------------------------------------------------------------------------- #
def policy():
    return PE.PolicyVersion(
        policy_version_id="policy_v1",
        property_id="prop_1",
        unit_scope="property",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        rules=PE.PolicyRules(
            currency="IDR",
            deposit_pct="30",
            service_fee_pct="5",
            tax_pbjt_pct="10",
            max_discount_pct="15",
            quote_valid_hours=48,
            hold_ttl_minutes=720,
            max_hold_extensions=2,
            payload={
                "extra_guest_fee_minor": 250_000,
                "cleaning_fee_minor": 500_000,
                "security_deposit_minor": 2_000_000,
                "min_stay_nights": 2,
                "cancellation_ladder": [{"min_days_before": 0, "refund_pct": "100"}],
            },
        ),
        created_at=NOW,
        source_doc_ref="policy/2026-public.pdf",
        approved_by=HUMAN,
    )


def rate_card():
    return QC.RateCard(
        rate_card_id="card_v1",
        property_id="prop_1",
        unit_scope="property",
        name="Public 2026",
        version=1,
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        currency="IDR",
        weekly_discount_pct="0",
        monthly_discount_pct="0",
        seasons=(
            QC.Season(
                season_id="season_regular",
                name="Regular",
                start_date="2026-01-01",
                end_date="2026-12-31",
                nightly_rate_minor=1_500_000,
                min_stay_nights=1,
                priority=0,
            ),
        ),
        source_doc_ref="rates/2026-public.pdf",
        approved_by=HUMAN,
    )


def priced(quote_id="quote_t1"):
    return QC.calculate_quote(
        QC.QuoteRequest(
            quote_date="2026-09-21",
            check_in=CHECK_IN,
            check_out=CHECK_OUT,
            adults=2,
            children=0,
            infants=0,
            extras=(),
            discount_minor=0,
        ),
        policy(),
        rate_card(),
        QC.UnitFacts(unit_id="unit_1", property_id="prop_1", base_capacity=2, max_guests=6),
        quote_id=quote_id,
        issued_at=NOW,
    )


@pytest.fixture()
def world():
    kernel = RK.ReservationKernel.open(
        ":memory:",
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed="w4c"),
        agency_id=AGENCY,
    )
    kernel.upsert_property(property_id="prop_1", name="Villa Canggu 1")
    kernel.upsert_unit(
        unit_id="unit_1", property_id="prop_1", name="Villa unit_1", base_capacity=2, max_guests=6
    )
    kernel.insert_policy_version(policy().as_row())
    kernel.insert_rate_card(
        {
            "rate_card_id": "card_v1",
            "property_id": "prop_1",
            "unit_scope": "property",
            "name": "Public 2026",
            "version": 1,
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "currency": "IDR",
            "weekly_discount_pct": "0",
            "monthly_discount_pct": "0",
            "source_doc_ref": "rates/2026-public.pdf",
            "approved_by": HUMAN,
            "created_at": NOW,
        },
        [
            {
                "season_id": "season_regular",
                "rate_card_id": "card_v1",
                "name": "Regular",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "nightly_rate_minor": 1_500_000,
                "min_stay_nights": 1,
                "priority": 0,
                "created_at": NOW,
            }
        ],
    )
    kernel.persist_quote(priced())
    yield kernel
    kernel.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def svc(kernel, *, agency_id=AGENCY):
    """A service per call, each with its own deterministic id sequence.

    Sharing one sequence across two service instances would make two audit rows
    collide on their id — the deterministic factory restarts at one for every
    instance, which is exactly what the house does with its ``w4b2``/``w4b2ai`` seeds.
    """
    return QT.QuotationService(
        kernel.conn,
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed=f"q{next(_SEQUENCES)}"),
        agency_id=agency_id,
    )


def ai(kernel):
    return AIS.AiService(
        kernel.conn,
        policy=PE.PolicyResolver(kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY),
        clock=FrozenClock.at(NOW),
        ids=IdFactory(deterministic=True, seed="w4cai"),
        agency_id=AGENCY,
    )


def principal(kernel):
    return ai(kernel).register_principal(
        "Reservation Agent", secret_ref="secret_ref_reservation_agent", actor=HUMAN
    )


def publish(kernel, tools, *, effective_from=EFFECTIVE_FROM):
    PE.PolicyResolver(kernel.conn, clock=FrozenClock.at(NOW), agency_id=AGENCY).publish(
        ALLOWANCE, list(tools), effective_from=effective_from, source_doc_ref=SOURCE, approved_by=HUMAN
    )


def drafted(kernel, *, quote_id="quote_t1"):
    return svc(kernel).draft(quote_id, actor=HUMAN, actor_role=HUMAN_ROLE)


def sent(kernel, *, quote_id="quote_t1"):
    version = drafted(kernel, quote_id=quote_id)
    return svc(kernel).send(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)


def snapshots_of(kernel, quotation_version_id):
    return {
        row["kind"]: row
        for row in kernel.conn.execute(
            "SELECT * FROM quotation_snapshots WHERE quotation_version_id = ?",
            (quotation_version_id,),
        )
    }


def state_of(kernel, quotation_version_id):
    return kernel.conn.execute(
        "SELECT status FROM quotation_versions WHERE quotation_version_id = ?",
        (quotation_version_id,),
    ).fetchone()["status"]


# --------------------------------------------------------------------------- #
# Versioning: V1, V2, V3 (§11)
# --------------------------------------------------------------------------- #
def test_draft_opens_version_one_for_the_stay(world):
    version = drafted(world)
    assert version.version == 1
    assert version.status == "DRAFT"
    assert version.unit_id == "unit_1"
    assert (version.check_in, version.check_out) == (CHECK_IN, CHECK_OUT)
    assert version.supersedes_id is None
    assert version.sent_at is None


def test_drafting_again_is_version_two_and_leaves_version_one_untouched(world):
    first = drafted(world)
    second = drafted(world)
    assert second.version == 2
    # ``supersedes_id`` names the offer the guest holds, and an unsent draft was never
    # one — so a second draft over a draft replaces nothing.
    assert second.supersedes_id is None
    # V1 is still the draft it was: a new version never rewrites the old one.
    assert svc(world).version(first.quotation_version_id).status == "DRAFT"


def test_a_new_draft_records_the_sent_version_it_will_replace(world):
    first = sent(world)
    second = drafted(world)
    assert second.supersedes_id == first.quotation_version_id
    # Recorded, not applied: V1 is still the live offer until V2 is actually sent.
    assert state_of(world, first.quotation_version_id) == "SENT"


def test_the_chain_is_readable_in_order(world):
    drafted(world)
    drafted(world)
    drafted(world)
    chain = svc(world).versions(unit_id="unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
    assert [version.version for version in chain] == [1, 2, 3]


def test_a_draft_over_an_unknown_quote_is_refused(world):
    with pytest.raises(DomainError) as caught:
        svc(world).draft("quote_missing", actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_QUOTE_NOT_FOUND"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_versions").fetchone()[0] == 0


def test_a_draft_over_another_agencys_quote_is_refused(world):
    with pytest.raises(DomainError) as caught:
        svc(world, agency_id=OTHER_AGENCY).draft(
            "quote_t1", actor=HUMAN, actor_role=HUMAN_ROLE
        )
    assert caught.value.code == "QUOTATION_QUOTE_NOT_FOUND"


def test_reading_another_agencys_version_is_refused(world):
    version = drafted(world)
    with pytest.raises(DomainError) as caught:
        svc(world, agency_id=OTHER_AGENCY).version(version.quotation_version_id)
    assert caught.value.code == "QUOTATION_NOT_FOUND"


# --------------------------------------------------------------------------- #
# Sending: freeze the record, then mark it sent
# --------------------------------------------------------------------------- #
def test_sending_writes_the_three_snapshots_and_marks_the_version_sent(world):
    version = sent(world)
    assert version.status == "SENT"
    assert version.sent_at == NOW_ISO
    snapshots = snapshots_of(world, version.quotation_version_id)
    assert set(snapshots) == {"PRICE", "TERMS", "POLICY"}
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 3


def test_each_snapshot_names_its_source_and_carries_a_fingerprint(world):
    version = sent(world)
    snapshots = snapshots_of(world, version.quotation_version_id)
    assert snapshots["PRICE"]["source_ref"] == "quote_t1"
    assert snapshots["TERMS"]["source_ref"] == "policy_v1"
    assert snapshots["POLICY"]["source_ref"] == "policy_v1"
    for row in snapshots.values():
        assert len(row["fingerprint"]) == 64
        assert json.loads(row["payload"])


def test_the_price_snapshot_holds_the_amounts_that_were_quoted(world):
    quote = priced()
    version = sent(world)
    payload = json.loads(snapshots_of(world, version.quotation_version_id)["PRICE"]["payload"])
    assert payload["total_minor"] == quote.total.minor_units
    assert payload["accommodation_minor"] == quote.accommodation.minor_units
    assert payload["quote_fingerprint"] == quote.fingerprint
    assert payload["currency"] == "IDR"


def test_the_terms_snapshot_holds_what_binds_the_guest(world):
    version = sent(world)
    payload = json.loads(snapshots_of(world, version.quotation_version_id)["TERMS"]["payload"])
    assert payload["deposit_pct"] == "30"
    assert payload["min_stay_nights"] == 2
    assert payload["cancellation_ladder"] == [{"min_days_before": 0, "refund_pct": "100"}]
    assert payload["expires_at"] == priced().expires_at


def test_the_policy_snapshot_holds_the_governing_identity(world):
    version = sent(world)
    payload = json.loads(snapshots_of(world, version.quotation_version_id)["POLICY"]["payload"])
    assert payload["policy_version_id"] == "policy_v1"
    assert payload["source_doc_ref"] == "policy/2026-public.pdf"
    assert payload["approved_by"] == HUMAN
    assert payload["fingerprint"] == policy().fingerprint


def test_sending_twice_is_refused(world):
    version = sent(world)
    with pytest.raises(DomainError) as caught:
        svc(world).send(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_ALREADY_SENT"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 3


def test_sending_version_two_supersedes_version_one(world):
    first = sent(world)
    second = drafted(world)
    svc(world).send(second.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert state_of(world, first.quotation_version_id) == "SUPERSEDED"
    assert state_of(world, second.quotation_version_id) == "SENT"
    # Superseding moves the status only: V1 still points at the quote it quoted.
    row = world.conn.execute(
        "SELECT quote_id FROM quotation_versions WHERE quotation_version_id = ?",
        (first.quotation_version_id,),
    ).fetchone()
    assert row["quote_id"] == "quote_t1"


def test_current_is_the_live_offer(world):
    first = sent(world)
    assert svc(world).current(unit_id="unit_1", check_in=CHECK_IN, check_out=CHECK_OUT).quotation_version_id == first.quotation_version_id
    second = drafted(world)
    svc(world).send(second.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    current = svc(world).current(unit_id="unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
    assert current.quotation_version_id == second.quotation_version_id
    assert current.version == 2


def test_an_accepted_version_is_never_superseded(world):
    first = sent(world)
    svc(world).accept(first.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    second = drafted(world)
    svc(world).send(second.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert state_of(world, first.quotation_version_id) == "ACCEPTED"


# --------------------------------------------------------------------------- #
# §56: sending is a client communication, so it is policy-controlled
# --------------------------------------------------------------------------- #
def test_a_human_may_send_without_any_policy_value(world):
    assert sent(world).status == "SENT"


def test_a_machine_actor_is_refused_while_the_allowance_is_unpublished(world):
    version = drafted(world)
    with pytest.raises(DomainError) as caught:
        svc(world).send(
            version.quotation_version_id,
            actor=MACHINE,
            actor_role=MACHINE_ROLE,
            principal_id=principal(world).principal_id,
        )
    assert caught.value.code == "AI_MEDIUM_TOOL_NOT_ALLOWED"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 0
    assert state_of(world, version.quotation_version_id) == "DRAFT"


def test_a_machine_actor_must_name_the_principal_it_acts_as(world):
    version = drafted(world)
    with pytest.raises(DomainError) as caught:
        svc(world).send(version.quotation_version_id, actor=MACHINE, actor_role=MACHINE_ROLE)
    assert caught.value.code == "MACHINE_PRINCIPAL_REQUIRED"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 0


def test_publishing_the_quotation_tool_admits_a_machine_actor(world):
    publish(world, [QT.SEND_TOOL])
    version = drafted(world)
    outcome = svc(world).send(
        version.quotation_version_id,
        actor=MACHINE,
        actor_role=MACHINE_ROLE,
        principal_id=principal(world).principal_id,
    )
    assert outcome.status == "SENT"
    assert len(snapshots_of(world, version.quotation_version_id)) == 3


def test_allowing_another_tool_alone_does_not_open_sending(world):
    publish(world, [OTHER_TOOL])
    version = drafted(world)
    with pytest.raises(DomainError) as caught:
        svc(world).send(
            version.quotation_version_id,
            actor=MACHINE,
            actor_role=MACHINE_ROLE,
            principal_id=principal(world).principal_id,
        )
    assert caught.value.code == "AI_MEDIUM_TOOL_NOT_ALLOWED"


def test_the_gate_runs_before_anything_is_written(world):
    version = drafted(world)
    with pytest.raises(DomainError):
        svc(world).send(version.quotation_version_id, actor=MACHINE, actor_role=MACHINE_ROLE)
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 0
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_acceptances").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# §11 immutability, enforced by the database rather than by the module
# --------------------------------------------------------------------------- #
def test_a_sent_versions_content_cannot_be_overwritten(world):
    version = sent(world)
    with pytest.raises(sqlite3.IntegrityError) as caught:
        world.conn.execute(
            "UPDATE quotation_versions SET quote_id = 'quote_other' WHERE quotation_version_id = ?",
            (version.quotation_version_id,),
        )
    assert "immutable" in str(caught.value)


def test_a_sent_versions_stay_cannot_be_overwritten(world):
    version = sent(world)
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "UPDATE quotation_versions SET check_out = '2026-10-09' WHERE quotation_version_id = ?",
            (version.quotation_version_id,),
        )


def test_a_sent_versions_status_may_still_move_forward(world):
    version = sent(world)
    world.conn.execute(
        "UPDATE quotation_versions SET status = 'SUPERSEDED' WHERE quotation_version_id = ?",
        (version.quotation_version_id,),
    )
    assert state_of(world, version.quotation_version_id) == "SUPERSEDED"


def test_a_draft_is_not_frozen_because_nothing_was_sent(world):
    version = drafted(world)
    world.conn.execute(
        "UPDATE quotation_versions SET created_by = 'user:other' WHERE quotation_version_id = ?",
        (version.quotation_version_id,),
    )
    row = world.conn.execute(
        "SELECT created_by FROM quotation_versions WHERE quotation_version_id = ?",
        (version.quotation_version_id,),
    ).fetchone()
    assert row["created_by"] == "user:other"


def test_a_version_cannot_be_deleted(world):
    version = drafted(world)
    with pytest.raises(sqlite3.IntegrityError) as caught:
        world.conn.execute(
            "DELETE FROM quotation_versions WHERE quotation_version_id = ?",
            (version.quotation_version_id,),
        )
    assert "append-only" in str(caught.value)


def test_snapshots_are_append_only(world):
    version = sent(world)
    snapshot_id = snapshots_of(world, version.quotation_version_id)["PRICE"]["snapshot_id"]
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "UPDATE quotation_snapshots SET payload = '{}' WHERE snapshot_id = ?", (snapshot_id,)
        )
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute("DELETE FROM quotation_snapshots WHERE snapshot_id = ?", (snapshot_id,))


def test_the_record_keeps_the_price_the_guest_agreed_to(world):
    """§11's point: the snapshot is the record, not a view of a mutable quote."""
    version = sent(world)
    before = json.loads(snapshots_of(world, version.quotation_version_id)["PRICE"]["payload"])
    world.conn.execute(
        "UPDATE quotes SET total_minor = total_minor + 1 WHERE quote_id = 'quote_t1'"
    )
    after = json.loads(snapshots_of(world, version.quotation_version_id)["PRICE"]["payload"])
    assert after["total_minor"] == before["total_minor"]


# --------------------------------------------------------------------------- #
# Acceptance: the five references §11 asks for
# --------------------------------------------------------------------------- #
def test_acceptance_names_the_version_its_three_snapshots_and_when(world):
    version = sent(world)
    acceptance = svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    snapshots = snapshots_of(world, version.quotation_version_id)
    assert acceptance.quotation_version_id == version.quotation_version_id
    assert acceptance.price_snapshot_id == snapshots["PRICE"]["snapshot_id"]
    assert acceptance.terms_snapshot_id == snapshots["TERMS"]["snapshot_id"]
    assert acceptance.policy_snapshot_id == snapshots["POLICY"]["snapshot_id"]
    assert acceptance.accepted_at == NOW_ISO
    assert state_of(world, version.quotation_version_id) == "ACCEPTED"


def test_acceptance_is_recorded_once(world):
    version = sent(world)
    svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    with pytest.raises(DomainError) as caught:
        svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_ALREADY_ACCEPTED"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_acceptances").fetchone()[0] == 1


def test_accepting_a_draft_is_refused(world):
    version = drafted(world)
    with pytest.raises(DomainError) as caught:
        svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_NOT_SENT"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_acceptances").fetchone()[0] == 0


def test_a_superseded_version_cannot_be_accepted(world):
    first = sent(world)
    second = drafted(world)
    svc(world).send(second.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    with pytest.raises(DomainError) as caught:
        svc(world).accept(first.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_NOT_SENT"


def test_an_accepted_version_cannot_gain_a_snapshot(world):
    version = sent(world)
    svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    with pytest.raises(sqlite3.IntegrityError) as caught:
        world.conn.execute(
            "INSERT INTO quotation_snapshots (snapshot_id, agency_id, quotation_version_id, kind,"
            " payload, fingerprint, created_at) VALUES ('snap_late', ?, ?, 'PRICE', '{}', ?, ?)",
            (AGENCY, version.quotation_version_id, "a" * 64, NOW),
        )
    assert "cannot gain a new snapshot" in str(caught.value)


def test_acceptance_is_append_only(world):
    version = sent(world)
    acceptance = svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "UPDATE quotation_acceptances SET accepted_at = '2026-01-01T00:00:00Z'"
            " WHERE acceptance_id = ?",
            (acceptance.acceptance_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        world.conn.execute(
            "DELETE FROM quotation_acceptances WHERE acceptance_id = ?", (acceptance.acceptance_id,)
        )


# --------------------------------------------------------------------------- #
# Withdrawal, vocabulary, and the audit trail
# --------------------------------------------------------------------------- #
def test_a_draft_can_be_voided_and_a_void_version_cannot_be_sent(world):
    version = drafted(world)
    assert svc(world).void(
        version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE, reason="guest withdrew"
    ).status == "VOID"
    with pytest.raises(DomainError) as caught:
        svc(world).send(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    assert caught.value.code == "QUOTATION_TRANSITION_REFUSED"
    assert world.conn.execute("SELECT COUNT(*) FROM quotation_snapshots").fetchone()[0] == 0


def test_a_void_version_does_not_supersede_and_is_not_current(world):
    first = sent(world)
    second = drafted(world)
    svc(world).void(
        second.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE, reason="price error"
    )
    assert state_of(world, first.quotation_version_id) == "SENT"
    current = svc(world).current(unit_id="unit_1", check_in=CHECK_IN, check_out=CHECK_OUT)
    assert current.quotation_version_id == first.quotation_version_id


def test_the_transition_table_refuses_an_edge_that_does_not_exist():
    assert QT.can_transition("DRAFT", "SENT")
    assert QT.can_transition("SENT", "ACCEPTED")
    assert QT.can_transition("SENT", "SUPERSEDED")
    assert not QT.can_transition("ACCEPTED", "SENT")
    assert not QT.can_transition("VOID", "SENT")
    assert not QT.can_transition("NONSENSE", "SENT")


def test_the_module_vocabulary_matches_the_schema(world):
    """The CHECK constraint and this module are two restatements of one vocabulary."""
    sql = world.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'quotation_versions'"
    ).fetchone()[0]
    check = re.search(r"status\s+TEXT\s+NOT NULL CHECK \(status IN \(([^)]*)\)\)", sql)
    assert check is not None, "the status CHECK is no longer where this test expects it"
    declared = set(re.findall(r"'([A-Z_]+)'", check.group(1)))
    assert declared == set(QT.QUOTATION_STATES)


def test_every_snapshot_kind_in_the_module_is_one_the_schema_allows(world):
    sql = world.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'quotation_snapshots'"
    ).fetchone()[0]
    declared = set(re.findall(r"'([A-Z_]+)'", sql))
    assert set(QT.SNAPSHOT_KINDS) <= declared


def test_the_audit_trail_names_the_states_it_moved_through(world):
    version = sent(world)
    svc(world).accept(version.quotation_version_id, actor=HUMAN, actor_role=HUMAN_ROLE)
    rows = {
        row["action"]: row
        for row in world.conn.execute(
            "SELECT action, after_state, before_state FROM audit_events"
            " WHERE entity_id = ? ORDER BY seq",
            (version.quotation_version_id,),
        )
    }
    assert json.loads(rows["quotation.drafted"]["after_state"])["status"] == "DRAFT"
    assert json.loads(rows["quotation.sent"]["after_state"])["status"] == "SENT"
    assert json.loads(rows["quotation.accepted"]["after_state"])["status"] == "ACCEPTED"
