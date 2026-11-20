"""ADR-0074 (PROPOSED) E5 acceptance: a booking closes only on a verified supplier callback,
signed by a bound reservation_manager with a fresh step-up.

Everything drives the real kernel and the real schema on a throwaway in-memory database. Time
only moves when a test moves it. Every refusal test snapshots the tables before and after and
asserts nothing was written; no test writes a booking status directly.

No test here weakens an existing gate, and none is skipped or marked XFAIL.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import identity as ID
import reservation_kernel as RK
import settings as SETTINGS
import supplier_assertion as SA
import totp as T
from agency_roles import AgencyRoles
from booking_support import ensure_supplier
from primitives import DomainError, IdFactory, parse_iso
from test_supplier_block_proof import SupplierProofCase

# identity -> (canonical role it is appointed to, or None for "never appointed")
PEOPLE = {
    "owner-1": "owner",
    "gm-1": "gm",
    "rm-1": "reservation_manager",
    "rm-2": "reservation_manager",
    "agent-1": None,  # appointed to nothing: any role it claims is an invention
}
PHONE = "+62 812 3456 7890"
TABLES = (
    "bookings", "booking_state_events", "audit_events", "supplier_block_proofs",
    "availability_evidence", "idempotency_keys", "supplier_contacts",
    "supplier_contact_events", "evidence_supplier_attributions", "auth_sessions",
)


class AssertionCase(SupplierProofCase):
    """A tenant with staffed roles, TOTP factors, one supplier, and helpers to sign in."""

    def setUp(self):
        super().setUp()
        self.secrets = {name: T.generate_secret() for name in PEOPLE}
        self.svc = ID.SessionService(
            self.kernel.conn, clock=self.kernel.clock, ids=IdFactory(deterministic=True, seed="id"),
            agency_id=self.kernel.agency_id,
            secrets=ID.MappingSecretResolver({f"{n}-ref": s for n, s in self.secrets.items()}),
        )
        roles = AgencyRoles(self.kernel.conn, self.kernel.clock, agency_id=self.kernel.agency_id)
        roles.seed_blueprint(actor="owner-1", actor_role="owner")
        for name, role in PEOPLE.items():
            if role is not None:
                roles.assign_holder(role, holder_ref=name, actor="owner-1", actor_role="owner")
            # a factor enrolled and confirmed *before* "now", so a login code is never a replay
            self.svc.enroll_totp(name, secret_ref=f"{name}-ref", at=self.at(-120))
            self.svc.confirm_totp(name, self.code(name, -120), at=self.at(-120))
        self.supplier_id, self.relationship_id = ensure_supplier(
            self.kernel, unit_id="unit_1", property_id="prop_1"
        )
        self.tokens: dict[str, str] = {}

    # -- time and codes ---------------------------------------------------- #
    def at(self, seconds: int) -> str:
        return ID.to_iso(self.kernel.clock.now() + timedelta(seconds=seconds))

    def code(self, name: str, seconds: int = 0) -> str:
        return T.totp(self.secrets[name], parse_iso(self.at(seconds)))

    def spendable_code(self, name: str) -> str:
        """A code for *now* that is not a replay: a spent step moves the clock to the next one."""
        step = int(self.kernel.clock.now().timestamp() // T.DEFAULT_STEP_SECONDS)
        used = self.__dict__.setdefault("_spent", {})
        if used.get(name, -1) >= step:
            self.kernel.clock.advance(seconds=T.DEFAULT_STEP_SECONDS)
            step += 1
        used[name] = step
        return self.code(name)

    # -- sessions ----------------------------------------------------------- #
    def login(self, name: str, role: str, *, step_up: bool = False) -> str:
        mfa = ID.is_mfa_role(role)
        result = self.svc.open_session(
            name, role, mfa_code=self.spendable_code(name) if mfa else None
        )
        if step_up:
            self.step_up(result.session.session_id, name)
        self.tokens[f"{name}:{role}"] = result.token
        return result.token

    def step_up(self, session_id: str, name: str) -> None:
        self.svc.satisfy_step_up(session_id, self.spendable_code(name))

    def rm(self, name: str = "rm-1") -> str:
        """A bound reservation_manager session that has just stepped up."""
        return self.login(name, "reservation_manager", step_up=True)

    # -- the supplier chain ---------------------------------------------------- #
    def verified_contact(self, *, key: str = "c1", supplier_id: str | None = None) -> str:
        owner = self.login("owner-1", "owner")
        gm = self.login("gm-1", "gm")
        contact = self.kernel.register_supplier_contact(
            supplier_id=supplier_id or self.supplier_id, contact_value=PHONE,
            registration_basis="SIGNED_AGREEMENT", session_token=owner,
            idempotency_key=f"idem_reg_{key}",
        )
        self.kernel.verify_supplier_contact(
            contact_id=contact, session_token=gm, idempotency_key=f"idem_ver_{key}"
        )
        return contact

    def enforce(self, on: bool = True) -> None:
        self.kernel.conn.execute(
            "INSERT OR REPLACE INTO agency_settings (setting_id, agency_id, setting_key, value,"
            " revision, updated_by, updated_role, updated_at) VALUES (?, ?, ?, ?, 1, 'owner-1',"
            " 'owner', ?)",
            ("set_enforce", self.kernel.agency_id, SA.SETTING_ENFORCED,
             "true" if on else "false", self.at(0)),
        )

    def assertion(self, booking, contact, token, **over):
        args = dict(
            booking_id=booking.booking_id, supplier_id=self.supplier_id,
            relationship_id=self.relationship_id, contact_id=contact,
            check_in=booking.check_in, check_out=booking.check_out,
            supplier_confirmed_at=self.at(-60), session_token=token,
            idempotency_key=over.pop("key", "idem_assert_1"),
        )
        args.update(over)
        return self.kernel.record_supplier_assertion(**args)

    def prove(self, booking, evidence, *, key="idem_proof_1"):
        return self.kernel.record_supplier_block_proof(
            booking_id=booking.booking_id, evidence_id=evidence, supplier_id=self.supplier_id,
            relationship_id=self.relationship_id, check_in=booking.check_in,
            check_out=booking.check_out, verification_method="MANAGER_CALL",
            verified_by="rm-1", actor="rm-1", verifier_role="reservation_manager",
            idempotency_key=key,
        )

    def ready(self):
        """Paid booking + verified contact + attributed evidence + recorded proof; enforcement on."""
        _, booking = self.paid()
        contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)
        evidence = self.assertion(booking, contact, self.rm())
        self.enforce()
        return booking, contact, evidence, self.prove(booking, evidence)

    # -- zero-write evidence ------------------------------------------------------ #
    def tables(self) -> dict[str, list]:
        conn = self.kernel.conn
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in TABLES}

    def refused(self, code: str, call, *args, **kwargs) -> None:
        """The call raises ``code`` and leaves every table above byte-for-byte as it was."""
        before = self.tables()
        with self.assertRaises((DomainError, ID.IdentityError)) as ctx:
            call(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.tables(), before, f"{code} left a partial write")

    def confirm(self, booking, proof, token, **over):
        return self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, session_token=token,
            command_id=over.pop("command_id", "cmd_confirm_e5"), **over,
        )


# --------------------------------------------------------------------------- #
# Checkpoint A: the data contract
# --------------------------------------------------------------------------- #
class TestContactRegistry(AssertionCase):
    def test_two_distinct_humans_register_and_check_a_contact(self):
        contact = self.verified_contact()
        row = self.kernel.conn.execute(
            "SELECT created_by, created_role FROM supplier_contacts WHERE contact_id = ?", (contact,)
        ).fetchone()
        self.assertEqual((row[0], row[1]), ("owner-1", "owner"))
        event = self.kernel.conn.execute(
            "SELECT actor, actor_role, kind FROM supplier_contact_events WHERE contact_id = ?",
            (contact,),
        ).fetchone()
        self.assertEqual(tuple(event), ("gm-1", "gm", "VERIFIED"))

    def test_the_creator_cannot_check_their_own_contact(self):
        owner = self.login("owner-1", "owner")
        contact = self.kernel.register_supplier_contact(
            supplier_id=self.supplier_id, contact_value=PHONE,
            registration_basis="OWNER_ATTESTED", session_token=owner, idempotency_key="idem_reg_s",
        )
        self.refused(
            "CONTACT_CHECKER_IS_MAKER", self.kernel.verify_supplier_contact,
            contact_id=contact, session_token=owner, idempotency_key="idem_ver_self",
        )

    def test_the_database_itself_refuses_a_self_check(self):
        owner = self.login("owner-1", "owner")
        contact = self.kernel.register_supplier_contact(
            supplier_id=self.supplier_id, contact_value=PHONE,
            registration_basis="OWNER_ATTESTED", session_token=owner, idempotency_key="idem_reg_d",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "INSERT INTO supplier_contact_events (event_id, agency_id, supplier_id, contact_id,"
                " seq, kind, actor, actor_role, contact_created_by, occurred_at)"
                " VALUES ('ev_x', ?, ?, ?, 1, 'VERIFIED', 'owner-1', 'owner', 'owner-1', ?)",
                (self.kernel.agency_id, self.supplier_id, contact, self.at(0)),
            )

    def test_only_owner_and_gm_register_contacts(self):
        for name in ("rm-1",):
            token = self.login(name, "reservation_manager")
            self.refused(
                "SIGNER_ROLE_NOT_ALLOWED", self.kernel.register_supplier_contact,
                supplier_id=self.supplier_id, contact_value=PHONE,
                registration_basis="SIGNED_AGREEMENT", session_token=token,
                idempotency_key="idem_reg_rm",
            )

    def test_free_text_cannot_establish_authority(self):
        owner = self.login("owner-1", "owner")
        self.refused(
            "CONTACT_BASIS_REFUSED", self.kernel.register_supplier_contact,
            supplier_id=self.supplier_id, contact_value=PHONE,
            registration_basis="the supplier told me so", session_token=owner,
            idempotency_key="idem_reg_ft",
        )
        with self.assertRaises(sqlite3.IntegrityError):  # and the schema says the same
            self.kernel.conn.execute(
                "INSERT INTO supplier_contacts (contact_id, agency_id, supplier_id, channel,"
                " contact_value, registration_basis, created_by, created_role, created_at)"
                " VALUES ('c_x', ?, ?, 'PHONE', '+628123456', 'trust me', 'owner-1', 'owner', ?)",
                (self.kernel.agency_id, self.supplier_id, self.at(0)),
            )

    def test_a_contact_cannot_link_across_tenants(self):
        conn = self.kernel.conn
        conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_b', 'B', 'ACTIVE', ?, ?)", (self.at(0), self.at(0)),
        )
        conn.execute(
            "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
            " VALUES ('sup_b', 'agency_b', 'Other tenant', 'ACTIVE', ?, ?)", (self.at(0), self.at(0)),
        )
        owner = self.login("owner-1", "owner")
        self.refused(
            "CONTACT_SUPPLIER_NOT_ACTIVE", self.kernel.register_supplier_contact,
            supplier_id="sup_b", contact_value=PHONE, registration_basis="SIGNED_AGREEMENT",
            session_token=owner, idempotency_key="idem_reg_x",
        )
        with self.assertRaises(sqlite3.IntegrityError):  # the composite FK, not the kernel
            conn.execute(
                "INSERT INTO supplier_contacts (contact_id, agency_id, supplier_id, channel,"
                " contact_value, registration_basis, created_by, created_role, created_at)"
                " VALUES ('c_x', ?, 'sup_b', 'PHONE', '+628123456', 'SIGNED_AGREEMENT', 'owner-1',"
                " 'owner', ?)", (self.kernel.agency_id, self.at(0)),
            )

    def test_the_number_is_immutable_and_the_history_append_only(self):
        contact = self.verified_contact()
        conn = self.kernel.conn
        for sql in (
            "UPDATE supplier_contacts SET contact_value = '+62000000000' WHERE contact_id = ?",
            "DELETE FROM supplier_contacts WHERE contact_id = ?",
            "UPDATE supplier_contact_events SET actor = 'x-1' WHERE contact_id = ?",
            "DELETE FROM supplier_contact_events WHERE contact_id = ?",
        ):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                conn.execute(sql, (contact,))

    def test_conflicting_verifications_collide_instead_of_both_succeeding(self):
        contact = self.verified_contact()
        gm = self.login("gm-1", "gm")  # a different human from the maker, checking a second time
        self.refused(
            "CONTACT_ALREADY_VERIFIED", self.kernel.verify_supplier_contact,
            contact_id=contact, session_token=gm, idempotency_key="idem_ver_race",
        )
        with self.assertRaises(sqlite3.IntegrityError):  # a second VERIFIED, written under the kernel
            self.kernel.conn.execute(
                "INSERT INTO supplier_contact_events (event_id, agency_id, supplier_id, contact_id,"
                " seq, kind, actor, actor_role, contact_created_by, occurred_at)"
                " VALUES ('ev_y', ?, ?, ?, 2, 'VERIFIED', 'gm-1', 'gm', 'owner-1', ?)",
                (self.kernel.agency_id, self.supplier_id, contact, self.at(0)),
            )

    def test_contact_read_is_audited_and_never_puts_the_number_in_the_trail(self):
        contact = self.verified_contact()
        read = self.kernel.read_supplier_contact_for_callback(
            contact_id=contact, session_token=self.rm()
        )
        self.assertEqual(read["contact_value"], PHONE)
        trail = " ".join(
            str(r[0]) for r in self.kernel.conn.execute("SELECT before_state FROM audit_events")
        ) + " ".join(
            str(r[0]) for r in self.kernel.conn.execute("SELECT after_state FROM audit_events")
        )
        self.assertNotIn("3456", trail)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'supplier_contact.read'"
            ).fetchone()[0], 1,
        )
        agent = self.login("agent-1", "reservation_agent")
        self.refused(
            "SIGNER_ROLE_NOT_ALLOWED", self.kernel.read_supplier_contact_for_callback,
            contact_id=contact, session_token=agent,
        )


# --------------------------------------------------------------------------- #
# Checkpoint B: the trust boundary
# --------------------------------------------------------------------------- #
class TestRoleBindingAndStepUp(AssertionCase):
    def setUp(self):
        super().setUp()
        _, self.booking = self.paid()
        self.contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)

    def test_a_bound_signer_with_fresh_step_up_records_the_assertion(self):
        evidence = self.assertion(self.booking, self.contact, self.rm())
        row = self.kernel.conn.execute(
            "SELECT callback_by, callback_role, source_kind FROM evidence_supplier_attributions"
            " WHERE evidence_id = ?", (evidence,),
        ).fetchone()
        self.assertEqual(tuple(row), ("rm-1", "reservation_manager", "CALLBACK_VERIFIED"))

    def test_a_caller_cannot_invent_reservation_manager_authority(self):
        # a real, MFA-less session whose identity was never appointed, claiming the role
        token = self.login("agent-1", "reservation_manager", step_up=True)
        self.refused("SIGNER_ROLE_NOT_BOUND", self.assertion, self.booking, self.contact, token)

    def test_a_role_held_by_someone_else_is_not_mine(self):
        # rm-1 is a reservation_manager; a session for agent-1 saying "gm" is not a gm
        token = self.login("agent-1", "gm")
        self.refused(
            "SIGNER_ROLE_NOT_ALLOWED", self.assertion, self.booking, self.contact, token
        )

    def test_a_role_that_is_not_a_signer_is_refused_even_when_bound(self):
        for name, role in (("owner-1", "owner"), ("gm-1", "gm")):
            token = self.login(name, role, step_up=True)
            self.refused(
                "SIGNER_ROLE_NOT_ALLOWED", self.assertion, self.booking, self.contact, token
            )

    def test_a_deactivated_holder_loses_authority_at_once(self):
        token = self.rm()
        AgencyRoles(self.kernel.conn, self.kernel.clock, agency_id=self.kernel.agency_id
                    ).deactivate_holder("reservation_manager", holder_ref="rm-1",
                                        actor="owner-1", actor_role="owner")
        self.refused("SIGNER_ROLE_NOT_BOUND", self.assertion, self.booking, self.contact, token)

    def test_a_code_for_one_identity_cannot_open_authority_for_another(self):
        other = self.svc.open_session("rm-2", "reservation_manager")
        with self.assertRaises(ID.InvalidCode):
            self.svc.satisfy_step_up(other.session.session_id, self.spendable_code("rm-1"))
        self.refused(  # rm-2 never spent a code of its own, so it holds no second factor at all
            "MFA_REQUIRED", self.assertion, self.booking, self.contact, other.token
        )

    def test_one_signers_step_up_is_not_another_signers(self):
        self.rm("rm-1")  # rm-1 stepped up
        second = self.svc.open_session("rm-2", "reservation_manager")
        self.refused(
            "MFA_REQUIRED", self.assertion, self.booking, self.contact, second.token
        )

    def test_a_stale_step_up_fails(self):
        token = self.rm()
        self.kernel.clock.advance(minutes=16)
        self.refused(
            "STEP_UP_REQUIRED", self.assertion, self.booking, self.contact, token,
            supplier_confirmed_at=self.at(-60),
        )

    def test_a_session_that_has_not_stepped_up_cannot_sign(self):
        token = self.login("rm-1", "reservation_manager")
        self.refused("MFA_REQUIRED", self.assertion, self.booking, self.contact, token)

    def test_no_token_or_a_forged_token_is_refused(self):
        self.refused("SESSION_REQUIRED", self.assertion, self.booking, self.contact, "")
        self.refused(
            "SESSION_NOT_FOUND", self.assertion, self.booking, self.contact, "not-a-real-token"
        )

    def test_a_revoked_session_cannot_sign(self):
        result = self.svc.open_session("rm-1", "reservation_manager")
        self.step_up(result.session.session_id, "rm-1")
        self.svc.revoke(result.session.session_id, reason="test")
        self.refused(
            "SESSION_REVOKED", self.assertion, self.booking, self.contact, result.token
        )

    def test_a_machine_session_cannot_sign(self):
        result = self.svc.open_session("ai:planner", "ai_service")
        self.refused(
            "MACHINE_PRINCIPAL_REFUSED", self.assertion, self.booking, self.contact, result.token
        )

    def test_a_session_for_another_tenant_is_not_found_here(self):
        conn = self.kernel.conn
        conn.execute(
            "INSERT INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES ('agency_b', 'B', 'ACTIVE', ?, ?)", (self.at(0), self.at(0)),
        )
        other = ID.SessionService(
            conn, clock=self.kernel.clock, ids=IdFactory(deterministic=True, seed="b"),
            agency_id="agency_b",
            secrets=ID.MappingSecretResolver({"rm-1-ref": self.secrets["rm-1"]}),
        )
        other.enroll_totp("rm-1", secret_ref="rm-1-ref", at=self.at(-120))
        other.confirm_totp("rm-1", self.code("rm-1", -120), at=self.at(-120))
        foreign = other.open_session("rm-1", "reservation_manager")
        self.refused(
            "SESSION_NOT_FOUND", self.assertion, self.booking, self.contact, foreign.token
        )


class TestAssertionContent(AssertionCase):
    def setUp(self):
        super().setUp()
        _, self.booking = self.paid()
        self.contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)
        self.token = self.rm()

    def test_wrong_dates_are_refused(self):
        self.refused(
            "BLOCK_PROOF_WRONG_DATES", self.assertion, self.booking, self.contact, self.token,
            check_out="2026-10-04",
        )

    def test_a_confirmation_in_the_future_is_refused(self):
        self.refused(
            "ASSERTION_CONFIRMATION_FUTURE", self.assertion, self.booking, self.contact,
            self.token, supplier_confirmed_at=self.at(3600),
        )

    def test_a_confirmation_older_than_the_window_is_refused(self):
        self.kernel.clock.advance(hours=25)
        token = self.rm()
        self.refused(
            "ASSERTION_CONFIRMATION_STALE", self.assertion, self.booking, self.contact, token,
            supplier_confirmed_at=self.at(-25 * 3600 + 60),
        )

    def test_a_confirmation_older_than_the_booking_is_refused(self):
        self.refused(
            "ASSERTION_PREDATES_BOOKING", self.assertion, self.booking, self.contact, self.token,
            supplier_confirmed_at="2026-09-20T00:00:00+00:00",
        )

    def test_an_unverified_contact_is_refused(self):
        owner = self.login("owner-1", "owner")
        fresh = self.kernel.register_supplier_contact(
            supplier_id=self.supplier_id, contact_value="+62 800 0000 0000",
            registration_basis="OWNER_ATTESTED", session_token=owner, idempotency_key="idem_reg_u",
        )
        self.refused(
            "CONTACT_NOT_VERIFIED", self.assertion, self.booking, fresh, self.token
        )

    def test_a_suspended_contact_is_refused(self):
        owner = self.login("owner-1", "owner")
        self.kernel.suspend_supplier_contact(
            contact_id=self.contact, reason="number changed hands", session_token=owner,
            idempotency_key="idem_sus_1",
        )
        self.refused("CONTACT_SUSPENDED", self.assertion, self.booking, self.contact, self.token)

    def test_a_revoked_contact_is_refused(self):
        owner = self.login("owner-1", "owner")
        self.kernel.revoke_supplier_contact(
            contact_id=self.contact, reason="number changed hands", session_token=owner,
            idempotency_key="idem_rev_1",
        )
        self.refused("CONTACT_REVOKED", self.assertion, self.booking, self.contact, self.token)

    def test_a_suspended_supplier_is_refused(self):
        self.kernel.conn.execute(
            "UPDATE suppliers SET status = 'SUSPENDED' WHERE supplier_id = ?", (self.supplier_id,)
        )
        self.refused(
            "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED", self.assertion, self.booking, self.contact,
            self.token,
        )

    def test_a_relationship_that_covers_only_the_property_never_authorises(self):
        self.kernel.conn.execute(
            "UPDATE villa_supplier_relationships SET unit_id = NULL"
            " WHERE villa_supplier_relationship_id = ?", (self.relationship_id,),
        )
        self.refused(
            "BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED", self.assertion, self.booking, self.contact,
            self.token,
        )

    def test_a_contact_of_another_supplier_is_refused(self):
        conn = self.kernel.conn
        conn.execute(
            "INSERT INTO suppliers (supplier_id, agency_id, name, status, created_at, updated_at)"
            " VALUES ('sup_2', ?, 'Second', 'ACTIVE', ?, ?)",
            (self.kernel.agency_id, self.at(0), self.at(0)),
        )
        other = self.verified_contact(key="c2", supplier_id="sup_2")
        self.refused("CONTACT_SUPPLIER_MISMATCH", self.assertion, self.booking, other, self.token)

    def test_replay_of_the_same_assertion_returns_the_same_evidence(self):
        first = self.assertion(self.booking, self.contact, self.token)
        again = self.assertion(self.booking, self.contact, self.token)
        self.assertEqual(first, again)
        self.assertEqual(
            self.kernel.conn.execute(
                "SELECT COUNT(*) FROM evidence_supplier_attributions"
            ).fetchone()[0], 1,
        )

    def test_the_same_key_with_a_different_payload_conflicts(self):
        self.assertion(self.booking, self.contact, self.token)
        self.refused(
            "IDEMPOTENCY_CONFLICT", self.assertion, self.booking, self.contact, self.token,
            supplier_confirmed_at=self.at(-30),
        )


# --------------------------------------------------------------------------- #
# Confirmation, enforced
# --------------------------------------------------------------------------- #
class TestConfirmationIsEnforced(AssertionCase):
    def test_the_whole_chain_confirms_and_writes_one_audit_row(self):
        booking, _, _, proof = self.ready()
        audits = self.kernel.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        outcome = self.confirm(booking, proof, self.rm())
        self.assertEqual(outcome.status, "CONFIRMED")
        self.assertFalse(outcome.replayed)
        row = self.kernel.conn.execute(
            "SELECT actor FROM audit_events WHERE action = 'booking.confirmed' AND entity_id = ?",
            (booking.booking_id,),
        ).fetchone()
        self.assertEqual(row[0], "rm-1")
        self.assertGreater(
            self.kernel.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], audits
        )

    def test_a_replay_confirms_nothing_twice(self):
        booking, _, _, proof = self.ready()
        token = self.rm()
        self.confirm(booking, proof, token)
        before = self.tables()
        again = self.confirm(booking, proof, token)
        self.assertTrue(again.replayed)
        self.assertEqual(self.tables(), before)

    def test_confirming_without_a_session_is_refused(self):
        booking, _, _, proof = self.ready()
        self.refused("SESSION_REQUIRED", self.confirm, booking, proof, None)
        self.refused(
            "SESSION_REQUIRED", self.kernel.confirm_booking, booking_id=booking.booking_id,
            block_proof_id=proof, command_id="cmd_direct_1", actor="rm-1",
            actor_role="reservation_manager",
        )

    def test_a_direct_kernel_caller_cannot_assert_a_role_instead_of_a_session(self):
        booking, _, _, proof = self.ready()
        bound = RK.ReservationKernel(
            self.kernel.conn, clock=self.kernel.clock, agency_id=self.kernel.agency_id,
            actor="rm-1", actor_role="reservation_manager",
        )
        before = self.tables()
        with self.assertRaises(DomainError) as ctx:
            bound.confirm_booking(
                booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_direct_2"
            )
        self.assertEqual(ctx.exception.code, "SESSION_REQUIRED")
        self.assertEqual(self.tables(), before)

    def test_a_non_signer_session_cannot_confirm(self):
        booking, _, _, proof = self.ready()
        for name, role in (("owner-1", "owner"), ("gm-1", "gm")):
            token = self.login(name, role, step_up=True)
            self.refused("SIGNER_ROLE_NOT_ALLOWED", self.confirm, booking, proof, token)

    def test_an_invented_role_cannot_confirm(self):
        booking, _, _, proof = self.ready()
        token = self.login("agent-1", "reservation_manager", step_up=True)
        self.refused("SIGNER_ROLE_NOT_BOUND", self.confirm, booking, proof, token)

    def test_a_stale_step_up_cannot_confirm(self):
        booking, _, _, proof = self.ready()
        token = self.rm()
        self.kernel.clock.advance(minutes=16)
        self.refused("STEP_UP_REQUIRED", self.confirm, booking, proof, token)

    def test_a_caller_named_actor_must_be_the_session_identity(self):
        booking, _, _, proof = self.ready()
        self.refused(
            "BLOCK_PROOF_ACTOR_NOT_BOUND", self.confirm, booking, proof, self.rm("rm-2"),
            actor="rm-1",
        )

    def test_legacy_unattributed_evidence_cannot_authorise_a_new_confirmation(self):
        _, booking = self.paid()
        # E3-style proof: generic AVAILABLE evidence, recorded before enforcement
        from booking_support import supplier_block_proof

        legacy = supplier_block_proof(self.kernel, booking.booking_id)
        before_enforce = self.kernel.get_supplier_block_proof(legacy)
        self.assertIsNotNone(before_enforce)
        self.enforce()
        self.refused(
            "BLOCK_PROOF_EVIDENCE_UNATTRIBUTED", self.confirm, booking, legacy, self.rm()
        )

    def test_generic_availability_cannot_become_a_proof_once_enforced(self):
        _, booking = self.paid()
        self.enforce()
        generic = self.kernel.record_evidence(
            unit_id=booking.unit_id, source_type="manager_written", evidence_level="B",
            result="available", can_confirm=True, source_ref="villa_block_letter",
        )
        self.kernel.clock.advance(minutes=1)
        self.refused("BLOCK_PROOF_EVIDENCE_UNATTRIBUTED", self.prove, booking, generic)

    def test_an_assertion_for_another_booking_cannot_back_this_one(self):
        _, first = self.paid(key="idem_e_f1")
        _, second = self.paid(
            key="idem_e_f2", unit_id="unit_2", check_in="2026-10-10", check_out="2026-10-12"
        )
        contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)
        evidence = self.assertion(first, contact, self.rm())
        self.enforce()
        self.refused("BLOCK_PROOF_WRONG_UNIT", self.prove, second, evidence)

    def test_the_contact_is_rechecked_at_confirmation(self):
        booking, contact, _, proof = self.ready()
        owner = self.login("owner-1", "owner")
        self.kernel.suspend_supplier_contact(
            contact_id=contact, reason="number changed hands", session_token=owner,
            idempotency_key="idem_sus_c",
        )
        self.refused("CONTACT_SUSPENDED", self.confirm, booking, proof, self.rm())

    def test_a_suspended_supplier_is_rechecked_at_confirmation(self):
        booking, _, _, proof = self.ready()
        token = self.rm()
        self.kernel.conn.execute(
            "UPDATE suppliers SET status = 'SUSPENDED' WHERE supplier_id = ?", (self.supplier_id,)
        )
        self.refused("BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED", self.confirm, booking, proof, token)

    def test_a_suspended_relationship_is_rechecked_at_confirmation(self):
        booking, _, _, proof = self.ready()
        token = self.rm()
        self.kernel.conn.execute(
            "UPDATE villa_supplier_relationships SET status = 'SUSPENDED'"
            " WHERE villa_supplier_relationship_id = ?", (self.relationship_id,),
        )
        self.refused("BLOCK_PROOF_SUPPLIER_NOT_AUTHORIZED", self.confirm, booking, proof, token)

    def test_the_supplier_confirmation_is_rechecked_for_age(self):
        booking, _, _, proof = self.ready()
        # proofs are immutable, so age is tested by tightening the per-agency window
        self.kernel.conn.execute(
            "INSERT OR REPLACE INTO agency_settings (setting_id, agency_id, setting_key, value,"
            " revision, updated_by, updated_role, updated_at) VALUES ('set_age', ?, ?, '1', 1,"
            " 'owner-1', 'owner', ?)", (self.kernel.agency_id, SA.SETTING_MAX_AGE_HOURS, self.at(0)),
        )
        self.kernel.clock.advance(hours=1)
        token = self.rm()
        self.refused("ASSERTION_CONFIRMATION_STALE", self.confirm, booking, proof, token)

    def test_the_attesting_signer_losing_the_role_blocks_confirmation(self):
        booking, _, _, proof = self.ready()
        token = self.rm("rm-2")
        AgencyRoles(self.kernel.conn, self.kernel.clock, agency_id=self.kernel.agency_id
                    ).deactivate_holder("reservation_manager", holder_ref="rm-1",
                                        actor="owner-1", actor_role="owner")
        self.refused("SIGNER_ROLE_NOT_BOUND", self.confirm, booking, proof, token)

    def test_unpaid_bookings_still_cannot_be_confirmed(self):
        _, booking = self.reserve(key="idem_e_unpaid")
        contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)
        self.enforce()
        self.refused(
            "PAYMENT_REQUIRED", self.confirm, booking, "proof_none", self.rm()
        )
        self.assertIsNotNone(contact)

    def test_enforcement_is_off_by_default_so_existing_flows_are_unchanged(self):
        from booking_support import supplier_block_proof

        _, booking = self.paid()
        proof = supplier_block_proof(self.kernel, booking.booking_id)
        outcome = self.kernel.confirm_booking(
            booking_id=booking.booking_id, block_proof_id=proof, command_id="cmd_legacy_ok",
            actor="user:reservation-1", actor_role="reservation",
        )
        self.assertEqual(outcome.status, "CONFIRMED")


class TestPolicyAsData(AssertionCase):
    def test_the_settings_are_catalogued_and_validated(self):
        for key in (SA.SETTING_ENFORCED, SA.SETTING_MAX_AGE_HOURS):
            self.assertIn(key, SETTINGS.SETTING_DEFS)
            self.assertFalse(SETTINGS.SETTING_DEFS[key].required_before_go_live)
        enforced = SETTINGS.SETTING_DEFS[SA.SETTING_ENFORCED].validate
        hours = SETTINGS.SETTING_DEFS[SA.SETTING_MAX_AGE_HOURS].validate
        self.assertEqual(enforced("TRUE"), "true")
        self.assertEqual(hours("24"), "24")
        for bad in ("yes", "1", ""):
            with self.assertRaises(SETTINGS.InvalidSettingValue):
                enforced(bad)
        for bad in ("0", "25", "abc"):
            with self.assertRaises(SETTINGS.InvalidSettingValue):
                hours(bad)

    def test_the_signer_allowlist_is_exactly_reservation_manager(self):
        import roles as ROLES

        self.assertEqual(ROLES.SIGNER_ROLES, ("reservation_manager",))
        for other in ("owner", "gm", "finance_manager", "auditor", "system_admin",
                      "reservation_agent", "partnership_manager", "ai_service"):
            self.assertNotIn(other, ROLES.SIGNER_ROLES)


# --------------------------------------------------------------------------- #
# The operator read models: what the screen is allowed to show
# --------------------------------------------------------------------------- #
class TestOperatorReadModels(AssertionCase):
    def test_the_list_masks_the_number_and_reports_standing(self):
        contact = self.verified_contact()
        rows = self.kernel.list_supplier_contacts(session_token=self.rm())
        self.assertEqual([row["contact_id"] for row in rows], [contact])
        self.assertEqual(rows[0]["standing"], "VERIFIED")
        self.assertTrue(rows[0]["masked_value"].endswith("7890"))
        self.assertNotIn("6281234567890", rows[0]["masked_value"])
        self.assertNotIn("3456", rows[0]["masked_value"])

    def test_standing_follows_the_latest_event(self):
        contact = self.verified_contact()
        owner = self.login("owner-1", "owner")
        self.kernel.suspend_supplier_contact(
            contact_id=contact, reason="number changed hands", session_token=owner,
            idempotency_key="idem_read_suspend",
        )
        rows = self.kernel.list_supplier_contacts(session_token=self.login("gm-1", "gm"))
        self.assertEqual(rows[0]["standing"], "SUSPENDED")
        self.kernel.revoke_supplier_contact(
            contact_id=contact, reason="supplier left the programme", session_token=owner,
            idempotency_key="idem_read_revoke",
        )
        rows = self.kernel.list_supplier_contacts(session_token=self.login("gm-1", "gm"))
        self.assertEqual(rows[0]["standing"], "REVOKED")
        self.assertEqual(rows[0]["revoke_reason"], "supplier left the programme")

    def test_a_pending_contact_reads_as_unverified(self):
        owner = self.login("owner-1", "owner")
        contact = self.kernel.register_supplier_contact(
            supplier_id=self.supplier_id, contact_value=PHONE,
            registration_basis="OWNER_ATTESTED", session_token=owner,
            idempotency_key="idem_read_pending",
        )
        rows = self.kernel.list_supplier_contacts(session_token=owner)
        self.assertEqual(rows[0]["contact_id"], contact)
        self.assertEqual(rows[0]["standing"], "UNVERIFIED")

    def test_a_role_that_may_not_read_the_registry_is_refused(self):
        self.verified_contact()
        with self.assertRaises(DomainError) as ctx:
            self.kernel.list_supplier_contacts(
                session_token=self.login("agent-1", "reservation_agent")
            )
        self.assertEqual(ctx.exception.code, "SIGNER_ROLE_NOT_ALLOWED")

    def test_no_token_reads_nothing(self):
        with self.assertRaises(DomainError) as ctx:
            self.kernel.list_supplier_contacts(session_token="")
        self.assertEqual(ctx.exception.code, "SESSION_REQUIRED")

    def test_another_tenants_contact_is_not_listed(self):
        from agency_roles import AgencyRoles
        from identity import SessionService, MappingSecretResolver, to_iso
        from primitives import IdFactory

        other = RK.ReservationKernel(
            self.kernel.conn, clock=self.kernel.clock, agency_id="agency_other"
        )
        other.conn.execute(
            "INSERT OR IGNORE INTO agencies (agency_id, name, created_at, updated_at)"
            " VALUES ('agency_other', 'Other', ?, ?)", (to_iso(self.kernel.clock.now()),) * 2
        )
        secrets = {"other-ref": T.generate_secret()}
        svc = SessionService(
            other.conn, clock=other.clock, ids=IdFactory(deterministic=True, seed="o"),
            agency_id="agency_other", secrets=MappingSecretResolver(secrets),
        )
        svc.enroll_totp("other-owner", secret_ref="other-ref", at=self.at(-120))
        svc.confirm_totp(
            "other-owner", T.totp(secrets["other-ref"], parse_iso(self.at(-120))), at=self.at(-120)
        )
        roles = AgencyRoles(other.conn, other.clock, agency_id="agency_other")
        roles.seed_blueprint(actor="other-owner", actor_role="owner")
        roles.assign_holder("owner", holder_ref="other-owner", actor="other-owner", actor_role="owner")
        token = svc.open_session(
            "other-owner", "owner",
            mfa_code=T.totp(secrets["other-ref"], parse_iso(self.at(0))), at=self.at(0),
        ).token
        self.assertEqual(other.list_supplier_contacts(session_token=token), ())
        # ... and this tenant's own contact never appears in the other tenant's list
        self.verified_contact()
        self.assertEqual(other.list_supplier_contacts(session_token=token), ())

    def test_the_assertion_state_is_absent_then_fresh_then_expired(self):
        _, booking = self.paid()
        self.assertIsNone(
            self.kernel.supplier_assertion_for_booking(
                booking_id=booking.booking_id, session_token=self.rm()
            )
        )
        contact = self.verified_contact()
        self.kernel.clock.advance(minutes=5)
        self.assertion(booking, contact, self.rm())

        fresh = self.kernel.supplier_assertion_for_booking(
            booking_id=booking.booking_id, session_token=self.rm()
        )
        self.assertIsNotNone(fresh)
        self.assertEqual(fresh["state"], "FRESH")
        self.assertEqual(fresh["callback_by"], "rm-1")
        self.assertEqual(fresh["callback_role"], "reservation_manager")

        self.kernel.clock.advance(hours=25)
        expired = self.kernel.supplier_assertion_for_booking(
            booking_id=booking.booking_id, session_token=self.rm()
        )
        self.assertEqual(expired["state"], "EXPIRED")


# --------------------------------------------------------------------------- #
# Checkpoint G1: the database boundary enforces the attribution contract
# --------------------------------------------------------------------------- #
class TestDatabaseBoundary(AssertionCase):
    """Raw SQL against the schema, with the kernel's own rows as the reference.

    These tests bypass ``record_supplier_assertion`` on purpose: the question is not
    whether the service refuses to write a wrong row (it does, tested above) but whether
    the database refuses it when the service is not the writer. Every case starts from a
    real, kernel-produced attribution and changes exactly one field.
    """

    def setUp(self):
        super().setUp()
        _, self.booking, self.evidence, _ = self.ready()
        self.contact = self.kernel.conn.execute(
            "SELECT contact_id FROM evidence_supplier_attributions WHERE evidence_id = ?",
            (self.evidence,),
        ).fetchone()[0]

    # -- fixtures ---------------------------------------------------------- #
    def _columns(self, table: str) -> list[str]:
        return [row[1] for row in self.kernel.conn.execute(f"PRAGMA table_info({table})")]

    def _valid(self) -> dict:
        columns = self._columns("evidence_supplier_attributions")
        row = self.kernel.conn.execute(
            f"SELECT {', '.join(columns)} FROM evidence_supplier_attributions WHERE evidence_id = ?",
            (self.evidence,),
        ).fetchone()
        self.assertIsNotNone(row, "the kernel did not write the attribution this test mutates")
        return dict(zip(columns, row))

    def _plant_evidence(self, evidence_id: str, *, unit_id: str | None = None, source_ref: str | None = None) -> None:
        """A second evidence row, so a negative case fails on its own FK and not on the PK."""
        columns = self._columns("availability_evidence")
        row = self.kernel.conn.execute(
            f"SELECT {', '.join(columns)} FROM availability_evidence WHERE evidence_id = ?",
            (self.evidence,),
        ).fetchone()
        values = dict(zip(columns, row))
        values["evidence_id"] = evidence_id
        if unit_id is not None:
            values["unit_id"] = unit_id
        if source_ref is not None:
            values["source_ref"] = source_ref
        self.kernel.conn.execute(
            f"INSERT INTO availability_evidence ({', '.join(columns)})"
            f" VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[c] for c in columns),
        )

    def _write(self, evidence_id: str, **over) -> None:
        values = self._valid()
        values["evidence_id"] = evidence_id
        values.update(over)
        self.kernel.conn.execute(
            f"INSERT INTO evidence_supplier_attributions ({', '.join(values)})"
            f" VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )

    def _refused_by_database(self, evidence_id: str, **over) -> str:
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self._write(evidence_id, **over)
        self.kernel.conn.rollback()
        return str(ctx.exception)

    # -- A / E: the referenced evidence must be the capture that was attributed -- #
    def test_evidence_of_another_unit_is_refused(self):
        self._plant_evidence("evd_other_unit")
        message = self._refused_by_database("evd_other_unit", unit_id="unit_9")
        self.assertIn("FOREIGN KEY", message.upper())

    def test_a_nonexistent_evidence_capture_is_refused(self):
        message = self._refused_by_database("evd_does_not_exist")
        self.assertIn("FOREIGN KEY", message.upper())

    # -- B: the source event must be a VERIFIED event, not merely an existing one -- #
    def test_the_kind_column_refuses_anything_but_verified(self):
        self._plant_evidence("evd_kind")
        message = self._refused_by_database("evd_kind", contact_event_kind="SUSPENDED")
        self.assertIn("CHECK", message.upper())

    def test_a_suspended_event_cannot_be_the_source_of_an_attribution(self):
        gm = self.login("gm-1", "gm")
        suspended = self.kernel.suspend_supplier_contact(
            contact_id=self.contact, reason="owner withdrew the number", session_token=gm,
            idempotency_key="idem_suspend_g1",
        )
        self._plant_evidence("evd_suspended")
        message = self._refused_by_database("evd_suspended", contact_event_id=suspended)
        self.assertIn("FOREIGN KEY", message.upper())

    def test_a_nonexistent_event_is_refused(self):
        self._plant_evidence("evd_no_event")
        message = self._refused_by_database("evd_no_event", contact_event_id="sce_missing")
        self.assertIn("FOREIGN KEY", message.upper())

    # -- D: no NULL may switch a foreign key off (SQLite skips NULL composites) -- #
    def test_a_null_event_kind_is_refused(self):
        self._plant_evidence("evd_null_kind")
        message = self._refused_by_database("evd_null_kind", contact_event_kind=None)
        self.assertIn("NOT NULL", message.upper())

    def test_a_null_event_id_is_refused(self):
        self._plant_evidence("evd_null_event")
        message = self._refused_by_database("evd_null_event", contact_event_id=None)
        self.assertIn("NOT NULL", message.upper())

    # -- C: another tenant cannot be named, even with a valid local row -- #
    def test_a_cross_tenant_attribution_is_refused(self):
        self._plant_evidence("evd_tenant")
        message = self._refused_by_database("evd_tenant", agency_id="agency_other")
        self.assertIn("FOREIGN KEY", message.upper())

    # -- A, second half: the block window must be this booking's own stay -- #
    def test_the_block_window_must_be_the_bookings_stay(self):
        self._plant_evidence("evd_stay")
        message = self._refused_by_database(
            "evd_stay", block_start="2030-01-01", block_end="2030-01-03"
        )
        self.assertIn("FOREIGN KEY", message.upper())

    # -- F: an attribution cannot be rewritten once written -- #
    def test_an_attribution_cannot_be_rewritten(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.conn.execute(
                "UPDATE evidence_supplier_attributions SET callback_by = 'someone-else'"
                " WHERE evidence_id = ?",
                (self.evidence,),
            )
        self.kernel.conn.rollback()
        self.assertEqual(self._valid()["callback_by"], "rm-1")

    # -- A, core: the capture must be a callback capture for *this* contact -- #
    def test_evidence_that_is_not_a_supplier_callback_is_refused(self):
        self._plant_evidence("evd_plain_note", source_ref="letter-from-owner")
        message = self._refused_by_database("evd_plain_note")
        self.assertIn("FOREIGN KEY", message.upper())

    def test_evidence_for_a_different_contact_is_refused(self):
        self._plant_evidence("evd_other_contact", source_ref="callback_verified:ctc_someone_else")
        message = self._refused_by_database("evd_other_contact")
        self.assertIn("FOREIGN KEY", message.upper())

    # -- the happy path still works, and the tree stays clean -- #
    def test_a_complete_row_still_inserts_and_the_tree_is_fk_clean(self):
        self._plant_evidence("evd_happy")
        self._write("evd_happy")
        stored = self.kernel.conn.execute(
            "SELECT contact_event_kind, source_kind FROM evidence_supplier_attributions"
            " WHERE evidence_id = ?",
            ("evd_happy",),
        ).fetchone()
        self.assertEqual(tuple(stored), ("VERIFIED", "CALLBACK_VERIFIED"))
        self.assertEqual(
            list(self.kernel.conn.execute("PRAGMA foreign_key_check")), [],
            "the attribution left a dangling reference somewhere in the tree",
        )
