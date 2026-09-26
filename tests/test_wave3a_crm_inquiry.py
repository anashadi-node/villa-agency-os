"""WAVE 3-A tests: references, guest master, inquiry and confirmed requirements.

Master Spec v1.1 §5 (inquiry → requirements), §6 (inquiry is the source of truth for the
current trip requirements; guest identity comes from Guest Master), §50 WAVE 3, and
Appendix B (`INQ-YYYY-NNNNN`, per agency).

The two rules these tests defend hardest:

* **a reference is a label, not a credential** (Appendix B) — looking one up must never
  widen what a caller can see, so the lookup stays agency-scoped like every other read;
* **AI-extracted is not verified** (§2, §12) — a requirement may only claim it came from
  intake when the intake field is genuinely `ACCEPTED` and belongs to the same inquiry.
"""

from __future__ import annotations

import pytest

import crm as CRM
import references as REF
from cases import CaseService
from primitives import FrozenClock, IdFactory
from schema_loader import connect_sqlite

START = "2026-09-26T08:00:00+00:00"
AGENCIES = ("agency_bali", "agency_jogja")


@pytest.fixture()
def conn():
    connection = connect_sqlite(":memory:")
    for agency in AGENCIES:
        connection.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, ?, 'ACTIVE', ?, ?) ON CONFLICT DO NOTHING",
            (agency, agency, START, START),
        )
    yield connection
    connection.close()


def services(conn, agency_id: str = "agency_bali", seed: str = "wave3a"):
    clock = FrozenClock.at(START)
    ids = IdFactory(deterministic=True, seed=seed)
    cases = CaseService(conn, clock, ids, agency_id=agency_id)
    return CRM.CrmService(conn, clock=clock, ids=ids, agency_id=agency_id, case_service=cases)


def an_inquiry(svc, *, email: str = "ada@example.com", key: str = "inq-key-0001", body: str = "Looking for a villa in Ubud."):
    guest_id = svc.register_guest("Ada Lovelace", email=email)
    return guest_id, svc.open_inquiry(
        guest_id=guest_id, channel="website", body=body, idempotency_key=key,
        content_class="P2",
    )


# --------------------------------------------------------------------------- #
# 1. Reference shape and numbering (Appendix B)
# --------------------------------------------------------------------------- #
def test_reference_shape_is_exactly_appendix_b():
    assert REF.format_reference("INQUIRY", 2026, 1) == "INQ-2026-00001"
    assert REF.format_reference("QUOTE", 2026, 42) == "QT-2026-00042"
    assert REF.format_reference("INCIDENT", 2026, 123456) == "INC-2026-123456"
    assert REF.parse_reference("RSV-2026-00007") == ("RSV", 2026, 7)


@pytest.mark.parametrize("bad", ["INQ-2026-1", "INQ-26-00001", "XXX-2026-00001", "INQ202600001",
                                "INQ-2026-0000A", "", "INQ-2026"])
def test_a_malformed_reference_is_rejected(bad):
    with pytest.raises(REF.ReferenceError):
        REF.parse_reference(bad)


def test_an_unknown_kind_has_no_prefix():
    with pytest.raises(REF.UnknownReferenceKind) as caught:
        REF.format_reference("TICKET", 2026, 1)
    assert "APPROVAL" in caught.value.details["known"]  # the kinds, not the prefixes


def test_numbering_is_per_agency_and_per_year(conn):
    bali = REF.ReferenceAllocator(conn, agency_id="agency_bali")
    jogja = REF.ReferenceAllocator(conn, agency_id="agency_jogja")
    bali.conn.execute("INSERT INTO cases (case_id, agency_id, channel, guest_ref, status,"
                      " priority, sla_policy, first_response_due_at, quote_due_at,"
                      " idempotency_key, reference, created_at, updated_at)"
                      " VALUES ('c1','agency_bali','website','guest_1','NEW','NORMAL','std',"
                      " '2026-09-26T09:00:00+00:00','2026-09-26T10:00:00+00:00','key-0000001',"
                      " 'INQ-2026-00001', ?, ?)", (START, START))
    assert bali.allocate("INQUIRY", year=2026) == "INQ-2026-00002"
    assert jogja.allocate("INQUIRY", year=2026) == "INQ-2026-00001"
    assert bali.allocate("INQUIRY", year=2027) == "INQ-2027-00001"
    assert bali.allocate("QUOTE", year=2026) == "QT-2026-00001"


def test_the_unique_index_is_what_makes_duplicates_impossible(conn):
    """The allocator probes; the index enforces. Prove the index is real."""
    svc = services(conn)
    svc.register_guest("First", email="a@example.com")
    conn.execute("INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority,"
                 " sla_policy, first_response_due_at, quote_due_at, idempotency_key, reference,"
                 " created_at, updated_at) VALUES ('c1','agency_bali','website','guest_1','NEW',"
                 " 'NORMAL','std','2026-09-26T09:00:00+00:00','2026-09-26T10:00:00+00:00',"
                 " 'key-0000001', 'INQ-2026-00009', ?, ?)", (START, START))
    with pytest.raises(Exception) as caught:
        conn.execute("INSERT INTO cases (case_id, agency_id, channel, guest_ref, status,"
                     " priority, sla_policy, first_response_due_at, quote_due_at, idempotency_key,"
                     " reference, created_at, updated_at) VALUES ('c2','agency_bali','website',"
                     " 'guest_2','NEW','NORMAL','std','2026-09-26T09:00:00+00:00',"
                     " '2026-09-26T10:00:00+00:00','key-0000002','INQ-2026-00009', ?, ?)",
                     (START, START))
    # SQLite names the columns, not the index, when the index was created separately.
    assert "UNIQUE constraint failed: cases.agency_id, cases.reference" in str(caught.value)
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'cases'")}
    assert "uq_cases_reference" in indexes, "the named guard must exist, not just any index"

    # Positive control: the same insert with a free reference is accepted, so the
    # refusal above came from the uniqueness and not from a broken statement.
    conn.execute("INSERT INTO cases (case_id, agency_id, channel, guest_ref, status, priority,"
                 " sla_policy, first_response_due_at, quote_due_at, idempotency_key, reference,"
                 " created_at, updated_at) VALUES ('c2','agency_bali','website','guest_2','NEW',"
                 " 'NORMAL','std','2026-09-26T09:00:00+00:00','2026-09-26T10:00:00+00:00',"
                 " 'key-0000002','INQ-2026-00010', ?, ?)", (START, START))


# --------------------------------------------------------------------------- #
# 2. Guest master
# --------------------------------------------------------------------------- #
def test_a_guest_is_normalised_before_it_is_stored(conn):
    svc = services(conn)
    guest_id = svc.register_guest("  Ada Lovelace ", email=" ADA@Example.COM ",
                                  phone="+62 812-3456-7890")
    guest = svc.guest(guest_id)
    assert guest["full_name"] == "Ada Lovelace"
    assert guest["email"] == "ada@example.com"
    assert guest["phone"] == "+6281234567890"


@pytest.mark.parametrize("kwargs,reason", [
    ({}, "email or a phone"),
    ({"email": "not-an-email"}, "not usable"),
    ({"phone": "12345"}, "not usable"),
])
def test_an_unusable_guest_is_refused(conn, kwargs, reason):
    with pytest.raises(CRM.RequirementRejected) as caught:
        services(conn).register_guest("Ada", **kwargs)
    assert reason in str(caught.value)


def test_a_second_guest_with_the_same_contact_is_refused_by_id(conn):
    svc = services(conn)
    first = svc.register_guest("Ada Lovelace", email="ada@example.com")
    with pytest.raises(CRM.DuplicateGuest) as caught:
        svc.register_guest("A. Lovelace", email="ADA@example.com")
    assert caught.value.details["guest_id"] == first
    svc.register_guest("Ada On The Phone", phone="+62 812 3456 7890")
    with pytest.raises(CRM.DuplicateGuest):
        svc.register_guest("Ada Phone Two", phone="+6281234567890")


def test_two_agencies_may_hold_the_same_guest_email(conn):
    bali = services(conn, "agency_bali", seed="b")
    jogja = services(conn, "agency_jogja", seed="j")
    bali.register_guest("Ada", email="ada@example.com")
    jogja.register_guest("Ada", email="ada@example.com")  # each agency owns its book


def test_a_guest_is_invisible_to_another_agency(conn):
    bali = services(conn, "agency_bali", seed="b")
    jogja = services(conn, "agency_jogja", seed="j")
    guest_id = bali.register_guest("Ada", email="ada@example.com")
    with pytest.raises(CRM.GuestNotFound):
        jogja.guest(guest_id)


# --------------------------------------------------------------------------- #
# 3. Inquiry
# --------------------------------------------------------------------------- #
def test_opening_an_inquiry_creates_the_case_and_the_reference(conn):
    svc = services(conn)
    guest_id, inquiry = an_inquiry(svc)
    assert inquiry["reference"] == "INQ-2026-00001"
    row = svc.inquiry(inquiry["case_id"])
    assert row["guest_id"] == guest_id
    assert row["guest_ref"] == guest_id
    assert row["status"] == "NEW"
    assert row["channel"] == "website"


def test_inquiry_references_increment(conn):
    svc = services(conn)
    guest_id = svc.register_guest("Ada", email="ada@example.com")
    first = svc.open_inquiry(guest_id=guest_id, channel="website", body="one",
                             idempotency_key="key-0000001", content_class="P2")
    second = svc.open_inquiry(guest_id=guest_id, channel="email", body="two",
                              idempotency_key="key-0000002", content_class="P2")
    assert (first["reference"], second["reference"]) == ("INQ-2026-00001", "INQ-2026-00002")


def test_an_inquiry_needs_a_known_guest(conn):
    svc = services(conn)
    with pytest.raises(CRM.GuestNotFound):
        svc.open_inquiry(guest_id="guest_nobody", channel="website", body="hello",
                         idempotency_key="key-0000001", content_class="P2")
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_replaying_an_inquiry_does_not_create_a_second_case(conn):
    svc = services(conn)
    guest_id = svc.register_guest("Ada", email="ada@example.com")
    svc.open_inquiry(guest_id=guest_id, channel="website", body="hello",
                     idempotency_key="key-0000001", content_class="P2")
    with pytest.raises(Exception) as caught:
        svc.open_inquiry(guest_id=guest_id, channel="website", body="hello",
                         idempotency_key="key-0000001", content_class="P2")
    assert "DUPLICATE_CASE" in str(caught.value) or "idempotency" in str(caught.value).lower()
    assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


def test_a_reference_lookup_stays_inside_the_agency(conn):
    """Appendix B: the reference is a label, not an authorization boundary."""
    bali = services(conn, "agency_bali", seed="b")
    jogja = services(conn, "agency_jogja", seed="j")
    _, inquiry = an_inquiry(bali)
    assert bali.inquiry_by_reference("INQ-2026-00001")["case_id"] == inquiry["case_id"]
    with pytest.raises(CRM.InquiryNotFound):
        jogja.inquiry_by_reference("INQ-2026-00001")


def test_open_inquiries_are_listed_for_a_guest(conn):
    svc = services(conn)
    guest_id, inquiry = an_inquiry(svc)
    assert [row["case_id"] for row in svc.open_inquiries_for_guest(guest_id)] == [inquiry["case_id"]]


# --------------------------------------------------------------------------- #
# 4. Confirmed requirements
# --------------------------------------------------------------------------- #
def test_a_requirement_is_recorded_as_active(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    result = svc.record_requirement(inquiry["case_id"], field_name="check_in", field_value="2026-11-02",
                                    value_type="date", recorded_by="agent_1")
    assert result["field_value"] == "2026-11-02"
    assert result["superseded"] is None
    assert [r["field_name"] for r in svc.requirements(inquiry["case_id"])] == ["check_in"]


def test_a_changed_requirement_supersedes_the_old_one_and_keeps_history(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    first = svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="4",
                                   value_type="integer", recorded_by="agent_1")
    second = svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="6",
                                    value_type="integer", recorded_by="agent_1")
    assert second["superseded"] == first["requirement_id"]
    active = svc.requirements(inquiry["case_id"])
    assert [(r["field_name"], r["field_value"]) for r in active] == [("guests", "6")]
    history = svc.requirements(inquiry["case_id"], include_history=True)
    assert len(history) == 2
    assert {row["status"] for row in history} == {"ACTIVE", "SUPERSEDED"}
    assert first["requirement_id"] in [r["requirement_id"] for r in history]


def test_the_database_also_allows_only_one_active_value_per_field(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="4",
                           value_type="integer", recorded_by="agent_1")
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO inquiry_requirements (requirement_id, agency_id, case_id, field_name,"
            " field_value, value_type, status, source, recorded_by, recorded_at)"
            " VALUES ('req_dup','agency_bali',?, 'guests','9','integer','ACTIVE','HUMAN_ENTRY',"
            " 'agent_1', ?)", (inquiry["case_id"], START))
    assert "UNIQUE constraint failed: inquiry_requirements.agency_id," \
           " inquiry_requirements.case_id, inquiry_requirements.field_name" in str(caught.value)
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'inquiry_requirements'")}
    assert "uq_requirement_active_field" in indexes


@pytest.mark.parametrize("field_value,value_type,word", [
    ("next week", "date", "ISO date"),
    ("four", "integer", "integer"),
    ("about 5k", "money", "decimal"),
    ("yes", "boolean", "true or false"),
    ("", "text", "no value"),
])
def test_a_typed_requirement_that_does_not_parse_is_refused(conn, field_value, value_type, word):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    with pytest.raises(CRM.RequirementRejected) as caught:
        svc.record_requirement(inquiry["case_id"], field_name="check_in",
                               field_value=field_value, value_type=value_type,
                               recorded_by="agent_1")
    assert word in str(caught.value)


def test_a_boolean_is_normalised(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    result = svc.record_requirement(inquiry["case_id"], field_name="airport_pickup",
                                    field_value="TRUE", value_type="boolean", recorded_by="agent_1")
    assert result["field_value"] == "true"


def test_a_withdrawn_requirement_is_kept_not_deleted(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    recorded = svc.record_requirement(inquiry["case_id"], field_name="check_in",
                                      field_value="2026-11-02", value_type="date",
                                      recorded_by="agent_1")
    svc.withdraw_requirement(recorded["requirement_id"], actor="agent_1")
    assert svc.requirements(inquiry["case_id"]) == ()
    history = svc.requirements(inquiry["case_id"], include_history=True)
    assert len(history) == 1 and history[0]["status"] == "WITHDRAWN"
    with pytest.raises(CRM.RequirementRejected) as caught:
        svc.withdraw_requirement(recorded["requirement_id"], actor="agent_1")
    assert caught.value.code == "STATE_TRANSITION_NOT_ALLOWED"


# --------------------------------------------------------------------------- #
# 5. AI-extracted is not verified (§2, §12)
# --------------------------------------------------------------------------- #
def an_intake_field(conn, svc, inquiry, *, status: str, case_id: str | None = None) -> str:
    """Seed one intake field straight into the table the extraction pipeline writes.

    Built from the actual DDL: an ACCEPTED field must name its human reviewer
    (`ck_intake_field_reviewed`), which is the same human-verification rule this wave
    is defending, so the fixture has to honour it too.
    """
    case_id = inquiry["case_id"] if case_id is None else case_id
    message_id = conn.execute("SELECT case_message_id FROM case_messages WHERE case_id = ? LIMIT 1",
                              (case_id,)).fetchone()[0]
    reviewed = status != "PROPOSED"
    conn.execute(
        "INSERT INTO intake_proposals (proposal_id, case_id, source_message_id, extractor,"
        " method, produced_at, status, payload, fingerprint, created_at)"
        " VALUES ('prop_1', ?, ?, 'rule-engine', 'rule', ?, 'PROPOSED', '{}', ?, ?)",
        (case_id, message_id, START, "a" * 64, START))
    conn.execute(
        "INSERT INTO intake_fields (intake_field_id, proposal_id, case_id, field_name,"
        " field_value, value_type, confidence, source_message_id, status, value_sha256,"
        " created_at, reviewed_by, reviewed_at)"
        " VALUES ('field_1', 'prop_1', ?, 'check_in', '2026-11-02', 'date', 90, ?, ?, ?, ?, ?, ?)",
        (case_id, message_id, status, "b" * 64, START,
         "agent_1" if reviewed else None, START if reviewed else None))
    return "field_1"


def test_only_an_accepted_intake_field_becomes_a_requirement(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    field_id = an_intake_field(conn, svc, inquiry, status="PROPOSED")
    with pytest.raises(CRM.RequirementRejected) as caught:
        svc.record_requirement(inquiry["case_id"], field_name="check_in",
                               field_value="2026-11-02", value_type="date", recorded_by="agent_1",
                               source="ACCEPTED_INTAKE", source_intake_field_id=field_id)
    assert caught.value.code == "VERIFICATION_REQUIRED"
    assert caught.value.details["intake_status"] == "PROPOSED"
    assert svc.requirements(inquiry["case_id"]) == ()


def test_an_accepted_intake_field_carries_its_provenance(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    field_id = an_intake_field(conn, svc, inquiry, status="ACCEPTED")
    recorded = svc.record_requirement(inquiry["case_id"], field_name="check_in",
                                      field_value="2026-11-02", value_type="date",
                                      recorded_by="agent_1", source="ACCEPTED_INTAKE",
                                      source_intake_field_id=field_id)
    row = svc.requirements(inquiry["case_id"])[0]
    assert row["source"] == "ACCEPTED_INTAKE"
    assert row["source_intake_field_id"] == field_id
    assert recorded["source"] == "ACCEPTED_INTAKE"


def test_intake_sourced_requirements_must_cite_something(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    with pytest.raises(CRM.RequirementRejected) as caught:
        svc.record_requirement(inquiry["case_id"], field_name="check_in",
                               field_value="2026-11-02", value_type="date", recorded_by="agent_1",
                               source="ACCEPTED_INTAKE")
    assert caught.value.code == "VERIFICATION_REQUIRED"


def test_an_intake_field_from_another_inquiry_cannot_be_cited(conn):
    svc = services(conn)
    guest_id = svc.register_guest("Ada", email="ada@example.com")
    first = svc.open_inquiry(guest_id=guest_id, channel="website", body="one",
                             idempotency_key="key-0000001", content_class="P2")
    second = svc.open_inquiry(guest_id=guest_id, channel="email", body="two",
                              idempotency_key="key-0000002", content_class="P2")
    field_id = an_intake_field(conn, svc, first, status="ACCEPTED")
    with pytest.raises(CRM.RequirementRejected) as caught:
        svc.record_requirement(second["case_id"], field_name="check_in",
                               field_value="2026-11-02", value_type="date", recorded_by="agent_1",
                               source="ACCEPTED_INTAKE", source_intake_field_id=field_id)
    assert caught.value.code == "RESOURCE_NOT_FOUND"


def test_requirements_are_not_writable_from_another_agency(conn):
    bali = services(conn, "agency_bali", seed="b")
    jogja = services(conn, "agency_jogja", seed="j")
    _, inquiry = an_inquiry(bali)
    with pytest.raises(CRM.InquiryNotFound):
        jogja.record_requirement(inquiry["case_id"], field_name="check_in",
                                 field_value="2026-11-02", value_type="date",
                                 recorded_by="agent_1")


def test_a_supersede_chain_keeps_every_step(conn):
    """A -> B -> C: the middle row is SUPERSEDED and still carries its own link."""
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    first = svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="2",
                                   value_type="integer", recorded_by="agent_1")
    second = svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="4",
                                    value_type="integer", recorded_by="agent_1")
    third = svc.record_requirement(inquiry["case_id"], field_name="guests", field_value="6",
                                   value_type="integer", recorded_by="agent_1")
    history = {row["requirement_id"]: row
               for row in svc.requirements(inquiry["case_id"], include_history=True)}
    assert history[first["requirement_id"]]["status"] == "SUPERSEDED"
    assert history[second["requirement_id"]]["status"] == "SUPERSEDED"
    assert history[second["requirement_id"]]["supersedes_requirement_id"] == first["requirement_id"]
    assert history[third["requirement_id"]]["status"] == "ACTIVE"
    assert history[third["requirement_id"]]["supersedes_requirement_id"] == second["requirement_id"]


def test_a_requirement_cannot_supersede_itself(conn):
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO inquiry_requirements (requirement_id, agency_id, case_id, field_name,"
            " field_value, value_type, status, source, supersedes_requirement_id, recorded_by,"
            " recorded_at) VALUES ('req_self','agency_bali',?, 'guests','2','integer','ACTIVE',"
            " 'HUMAN_ENTRY','req_self','agent_1', ?)", (inquiry["case_id"], START))
    assert "ck_requirements_not_self_superseding" in str(caught.value)


def test_a_confirmed_requirement_cannot_hold_nothing(conn):
    """The service refuses an empty value; the table refuses one too."""
    svc = services(conn)
    _, inquiry = an_inquiry(svc)
    with pytest.raises(Exception) as caught:
        conn.execute(
            "INSERT INTO inquiry_requirements (requirement_id, agency_id, case_id, field_name,"
            " field_value, value_type, status, source, recorded_by, recorded_at)"
            " VALUES ('req_empty','agency_bali',?, 'guests', NULL,'integer','ACTIVE',"
            " 'HUMAN_ENTRY','agent_1', ?)", (inquiry["case_id"], START))
    assert "ck_requirements_value_present" in str(caught.value)
