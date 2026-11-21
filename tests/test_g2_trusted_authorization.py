"""G2-A — a close is a human decision, and the session is the only evidence of one.

Every test here drives the real ``SessionService`` and the real agency role table: there
is no test-only bypass, no arbitrary ``Principal`` and no actor string that the kernel
simply believes. The matrix runs against **both** states of
``reservation.supplier_assertion.enforced`` because the flag governs evidence, never
authority (pack §4).
"""

import unittest
from types import SimpleNamespace

import identity as ID
import supplier_assertion as SA
import totp as T
from agency_roles import AgencyRoles
from booking_support import SIGNER_IDENTITY, ensure_supplier, signer_token, supplier_block_proof
from primitives import DomainError
from test_supplier_block_proof import SupplierProofCase

SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
_ENROLLED: dict[tuple[int, str], bool] = {}



def _service(kernel, identity="rm-1"):
    return ID.SessionService(
        kernel.conn,
        clock=kernel.clock,
        ids=kernel.ids,
        agency_id=kernel.agency_id,
        secrets=ID.MappingSecretResolver({f"{identity}-ref": SECRET}),
    )


class TrustedAuthorizationCase(SupplierProofCase):
    def setUp(self):
        super().setUp()
        ensure_supplier(self.kernel, unit_id="unit_1", property_id="prop_1")

    def flag(self, enforced: bool) -> None:
        self.kernel.conn.execute(
            "INSERT OR REPLACE INTO agency_settings (setting_id, agency_id, setting_key,"
            " value, revision, updated_by, updated_role, updated_at)"
            " VALUES (?, ?, ?, ?, 1, 'owner-1', 'owner', ?)",
            (
                "set_enforce",
                self.kernel.agency_id,
                SA.SETTING_ENFORCED,
                "true" if enforced else "false",
                ID.to_iso(self.kernel.clock.now()),
            ),
        )
        self.kernel.conn.commit()

    def appoint(self, role: str, identity: str) -> None:
        roles = AgencyRoles(self.kernel.conn, self.kernel.clock, agency_id=self.kernel.agency_id)
        try:
            roles.seed_blueprint(actor="owner-1", actor_role="owner")
        except Exception:
            pass
        try:
            roles.assign_holder(role, holder_ref=identity, actor="owner-1", actor_role="owner")
        except Exception:
            pass

    def open_session(self, identity="rm-1", role="reservation_manager", *, step_up=True):
        cache = getattr(self.kernel, "_g2a_sessions", None)
        if cache is None:
            cache = {}
            self.kernel._g2a_sessions = cache
        key = (identity, role)
        if key in cache:
            return cache[key]
        if identity == SIGNER_IDENTITY:
            # The shared signer already holds a session for this database, and a TOTP code
            # is single-use: borrow that session instead of replaying the code.
            svc = _service(self.kernel, identity)
            token = signer_token(self.kernel)
            session = svc.authenticate(token)
            opened = SimpleNamespace(token=token, session=session)
            cache[key] = (svc, opened)
            return svc, opened
        self.appoint(role, identity)
        svc = _service(self.kernel, identity)
        if not _ENROLLED.get((id(self.kernel.conn), identity)):
            # Enroll and confirm the factor five minutes back, with the code of that
            # moment, so the login below still owns the current step's code.
            base = self.kernel.clock.now() - ID.timedelta(minutes=5)
            try:
                svc.enroll_totp(identity, secret_ref=f"{identity}-ref", at=base)
                svc.confirm_totp(identity, T.totp(SECRET, base), at=base)
            except Exception:
                pass
            _ENROLLED[(id(self.kernel.conn), identity)] = True
        code = T.totp(SECRET, self.kernel.clock.now())
        opened = svc.open_session(identity, role, mfa_code=code)
        if step_up:
            svc.satisfy_step_up(opened.session.session_id, code)
        cache[key] = (svc, opened)
        return svc, opened

    def paid_booking(self, *, key="idem_g2_0001", **window):
        _, booking = self.paid(key=key, **window)
        return booking

    def proof_for(self, booking):
        return supplier_block_proof(self.kernel, booking.booking_id,
                                    idempotency_key=f"sbp_g2_{booking.booking_id}")

    def close(self, booking, proof, **overrides):
        arguments = dict(
            booking_id=booking.booking_id,
            block_proof_id=proof,
            command_id="cmd_g2_confirm",
        )
        arguments.update(overrides)
        return self.kernel.confirm_booking(**arguments)

    def status_of(self, booking) -> str:
        return self.kernel.get_booking(booking.booking_id)["status"]

    def refusal(self, booking, proof, ticket=None, **overrides):
        if ticket is not None:
            overrides["session_token"] = ticket
        with self.assertRaises(DomainError) as ctx:
            self.close(booking, proof, **overrides)
        return ctx.exception.code


class TestNoSessionMeansNoClose(TrustedAuthorizationCase):
    """G2-T01, G2-T02, G2-T12 — absence of a session decides, and the flag cannot vote."""

    def test_T01_a_close_without_a_session_is_refused(self):
        for enforced in (False, True):
            with self.subTest(enforced=enforced):
                self.flag(enforced)
                booking = self.paid_booking(
                    key=f"idem_g2_t01_{int(enforced)}",
                    check_in="2026-11-01" if enforced else "2026-10-01",
                    check_out="2026-11-04" if enforced else "2026-10-04",
                )
                # No proof is recorded either: the refusal must come before the kernel
                # even looks at evidence, which is why the flag cannot matter here.
                self.assertEqual(
                    self.refusal(booking, "proof_not_recorded"), "SESSION_REQUIRED"
                )
                self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T02_caller_supplied_actor_and_role_are_not_authority(self):
        for enforced in (False, True):
            with self.subTest(enforced=enforced):
                self.flag(enforced)
                booking = self.paid_booking(
                    key=f"idem_g2_t02_{int(enforced)}",
                    check_in="2026-11-10" if enforced else "2026-10-10",
                    check_out="2026-11-13" if enforced else "2026-10-13",
                )
                self.assertEqual(
                    self.refusal(
                        booking, "proof_not_recorded",
                        actor="human:rm-1", actor_role="reservation_manager",
                    ),
                    "SESSION_REQUIRED",
                )
                self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T12_the_flag_changes_evidence_not_authority(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t12_false")
        off = self.refusal(booking, "proof_not_recorded")
        self.flag(True)
        on = self.refusal(booking, "proof_not_recorded")
        self.assertEqual((off, on), ("SESSION_REQUIRED", "SESSION_REQUIRED"))


class TestTheSessionMustBeGood(TrustedAuthorizationCase):
    """G2-T03, G2-T05, G2-T06, G2-T07 — a session that exists is not a session that holds."""

    def test_T03_a_role_without_signing_capability_is_refused(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t03")
        proof = self.proof_for(booking)
        _, opened = self.open_session(identity="agent-1", role="reservation_agent")
        self.assertEqual(
            self.refusal(booking, proof, ticket=opened.token), "SIGNER_ROLE_NOT_ALLOWED"
        )
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T05_an_expired_session_cannot_close(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t05")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        # Age the session itself: the kernel clock stays put, the row does not.
        # Move the clock past the idle timeout instead of editing the row: the row's own
        # CHECK constraints decide what a session may look like, and we do not touch them.
        self.kernel.clock.set(
            ID.to_iso(
                self.kernel.clock.now() + ID.SESSION_IDLE_TIMEOUT + ID.timedelta(minutes=1)
            )
        )
        self.assertEqual(self.refusal(booking, proof, ticket=opened.token), "SESSION_EXPIRED")

    def test_T06_a_revoked_session_cannot_close(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t06")
        proof = self.proof_for(booking)
        svc, opened = self.open_session()
        svc.revoke(opened.session.session_id, reason="laptop lost")
        self.assertEqual(self.refusal(booking, proof, ticket=opened.token), "SESSION_REVOKED")

    def test_T07_a_stale_step_up_cannot_close(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t07")
        proof = self.proof_for(booking)
        _, opened = self.open_session(identity="rm-2", step_up=False)
        self.assertIn(
            self.refusal(booking, proof, ticket=opened.token),
            ("STEP_UP_REQUIRED", "BLOCK_PROOF_STEP_UP_REQUIRED", "MFA_REQUIRED"),
        )


class TestOnlyTheReservationManagerSigns(TrustedAuthorizationCase):
    """G2-T04, G2-T08 — a machine has no authority, and a claim cannot borrow a session."""

    def test_T04_a_machine_kernel_is_refused_even_with_a_human_session(self):
        import reservation_kernel as RK

        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t04")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        machine = RK.ReservationKernel(
            self.kernel.conn,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            actor="ai:planner",
            actor_role="ai_service",
        )
        with self.assertRaises(DomainError) as ctx:
            machine.confirm_booking(
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_g2_t04",
                session_token=opened.token,
            )
        self.assertIn(
            ctx.exception.code, ("BLOCK_PROOF_MACHINE_VERIFIER", "MACHINE_PRINCIPAL_REFUSED")
        )
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T08_a_claim_cannot_borrow_another_identity(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t08")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        self.assertEqual(
            self.refusal(booking, proof, ticket=opened.token, actor="human:ops-9"),
            "BLOCK_PROOF_ACTOR_NOT_BOUND",
        )
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")


class TestTheAuthorizedPathStillWorks(TrustedAuthorizationCase):
    """G2-T09, G2-T10, G2-T11 — authorize, then behave exactly as before."""

    def test_T09_an_authorized_confirmation_closes_with_an_audit_trail(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t09")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        outcome = self.close(booking, proof, session_token=opened.token)
        self.assertEqual(outcome.status, "CONFIRMED")
        self.assertEqual(self.status_of(booking), "CONFIRMED")
        self.assertTrue(self.kernel.verify_audit_chain())
        self.assertIn("booking.confirmed", [row["action"] for row in self.kernel.audit_trail()])

    def test_T10_an_unauthorized_replay_does_not_disclose_the_booking(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t10")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        self.close(booking, proof, session_token=opened.token)
        with self.assertRaises(DomainError) as ctx:
            self.kernel.confirm_booking(
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_g2_replay",
                actor="human:rm-1",
                actor_role="reservation_manager",
            )
        self.assertEqual(ctx.exception.code, "SESSION_REQUIRED")
        self.assertIsNone(ctx.exception.details.get("booking"))

    def test_T11_a_refusal_writes_nothing(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t11")
        proof = self.proof_for(booking)
        before = self.snapshots(booking.booking_id)
        self.assertEqual(self.refusal(booking, proof), "SESSION_REQUIRED")
        self.assertEqual(self.snapshots(booking.booking_id), before)


if __name__ == "__main__":
    unittest.main()
