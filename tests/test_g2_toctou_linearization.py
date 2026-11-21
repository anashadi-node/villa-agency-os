"""G2-A T13-T16: authorization must linearize inside the write transaction.

The TOCTOU candidate from the REVIEW-003 pack: the pre-check runs outside the write
transaction, so a session (or role) revoked between the pre-check and ``BEGIN IMMEDIATE``
must not ride stale authority into the commit. The fix re-proves authority inside the
transaction (same boundary ``confirm_booking`` already used). These tests use a real
second connection — a concurrent revoker — and a deterministic interleave hook, not sleeps.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest import mock

import identity as ID
import reservation_kernel as RK
from primitives import DomainError

from booking_support import SECRET, SIGNER_IDENTITY, ensure_supplier
from test_g2_trusted_authorization import _ENROLLED, TrustedAuthorizationCase


class LinearizationCase(TrustedAuthorizationCase):
    """A file-backed database so a second connection can act as the concurrent writer."""

    def setUp(self):
        if not hasattr(self, "_db_dir"):
            self._db_dir = tempfile.TemporaryDirectory()
            self.addCleanup(self._db_dir.cleanup)
            self.db_path = f"{self._db_dir.name}/linearize.db"
        super().setUp()
        _ENROLLED.pop((id(self.kernel.conn), SIGNER_IDENTITY), None)
        self.peer = sqlite3.connect(self.db_path)
        self.peer.row_factory = sqlite3.Row
        self.addCleanup(self.peer.close)

    def peer_service(self, identity):
        return ID.SessionService(
            self.peer,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            agency_id=self.kernel.agency_id,
            secrets=ID.MappingSecretResolver({f"{identity}-ref": SECRET}),
        )

    def revoke_between_checks(self, identity):
        """Wrap ``_authorize_confirmation`` so the peer revokes between call 1 and 2.

        Call 1 is the pre-check outside the transaction; call 2 is the re-check inside it.
        The revoker commits on the second connection right after the pre-check returns and
        before the kernel's ``BEGIN IMMEDIATE`` — exactly the interleaving a concurrent
        revoker can win against an authorization that is checked only once.
        """
        svc, opened = self.open_session(identity=identity)
        calls = {"n": 0}
        original = RK.ReservationKernel._authorize_confirmation

        def interleave(kernel, conn, **kwargs):
            calls["n"] += 1
            result = original(kernel, conn, **kwargs)
            if calls["n"] == 1:
                # ``SessionService.revoke`` leaves its write transaction open; the commit
                # is what makes the revocation visible to the kernel's next BEGIN.
                self.peer_service(identity).revoke(
                    opened.session.session_id, reason="laptop stolen mid-shift"
                )
                self.peer.commit()
            return result

        return svc, opened, mock.patch.object(
            RK.ReservationKernel, "_authorize_confirmation", interleave
        )


class TestTheWriteTransactionReprovesAuthority(LinearizationCase):
    """G2-T13, G2-T14 — no privileged write may commit on authorization gone stale."""

    def test_T13_a_session_revoked_mid_flight_cannot_record_a_proof(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t13")
        supplier, relationship = ensure_supplier(
            self.kernel, unit_id=booking.unit_id, property_id=f"prop_{booking.unit_id}"
        )
        evidence = self.kernel.record_evidence(
            unit_id=booking.unit_id,
            source_type="manager_written",
            evidence_level="B",
            result="available",
            can_quote=True,
            can_hold=True,
            can_confirm=True,
            source_ref="villa_block_letter",
        )
        _, opened, interleave = self.revoke_between_checks(SIGNER_IDENTITY)
        with interleave, self.assertRaises(DomainError) as ctx:
            self.kernel.record_supplier_block_proof(
                booking_id=booking.booking_id,
                evidence_id=evidence,
                supplier_id=supplier,
                relationship_id=relationship,
                check_in=booking.check_in,
                check_out=booking.check_out,
                verification_method="MANAGER_CALL",
                verified_by=SIGNER_IDENTITY,
                actor=SIGNER_IDENTITY,
                verifier_role="reservation_manager",
                session_token=opened.token,
                idempotency_key="idem_sbp_t13",
            )
        self.assertEqual(ctx.exception.code, "SESSION_REVOKED")
        self.assertIsNone(
            self.kernel.conn.execute(
                "SELECT proof_id FROM supplier_block_proofs WHERE booking_id = ?",
                (booking.booking_id,),
            ).fetchone(),
            "a stale authorization must not leave a proof behind",
        )
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T14_a_session_revoked_mid_flight_cannot_revoke_a_proof(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t14")
        proof = self.proof_for(booking)
        _, opened, interleave = self.revoke_between_checks(SIGNER_IDENTITY)
        with interleave, self.assertRaises(DomainError) as ctx:
            self.kernel.revoke_supplier_block_proof(
                proof_id=proof,
                reason="dates changed",
                actor=SIGNER_IDENTITY,
                actor_role="reservation_manager",
                session_token=opened.token,
            )
        self.assertEqual(ctx.exception.code, "SESSION_REVOKED")
        row = self.kernel.conn.execute(
            "SELECT revoked_at, revoke_reason FROM supplier_block_proofs WHERE proof_id = ?",
            (proof,),
        ).fetchone()
        self.assertIsNone(row["revoked_at"], "stale authority must not revoke a proof")
        self.assertIsNone(row["revoke_reason"])

    def test_T15_a_role_demoted_mid_flight_cannot_record_a_proof(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t15")
        supplier, relationship = ensure_supplier(
            self.kernel, unit_id=booking.unit_id, property_id=f"prop_{booking.unit_id}"
        )
        evidence = self.kernel.record_evidence(
            unit_id=booking.unit_id,
            source_type="manager_written",
            evidence_level="B",
            result="available",
            can_quote=True,
            can_hold=True,
            can_confirm=True,
            source_ref="villa_block_letter",
        )
        _, opened, interleave = self.revoke_between_checks(SIGNER_IDENTITY)
        calls = {"n": 0}
        original = RK.ReservationKernel._authorize_confirmation

        def interleave_role(kernel, conn, **kwargs):
            calls["n"] += 1
            result = original(kernel, conn, **kwargs)
            if calls["n"] == 1:
                self.peer.execute(
                    "UPDATE agency_role_holders SET status = 'INACTIVE'"
                    " WHERE holder_ref = ? AND status = 'ACTIVE'",
                    (SIGNER_IDENTITY,),
                )
                self.peer.commit()
            return result

        with mock.patch.object(RK.ReservationKernel, "_authorize_confirmation", interleave_role):
            with self.assertRaises(DomainError) as ctx:
                self.kernel.record_supplier_block_proof(
                    booking_id=booking.booking_id,
                    evidence_id=evidence,
                    supplier_id=supplier,
                    relationship_id=relationship,
                    check_in=booking.check_in,
                    check_out=booking.check_out,
                    verification_method="MANAGER_CALL",
                    verified_by=SIGNER_IDENTITY,
                    actor=SIGNER_IDENTITY,
                    verifier_role="reservation_manager",
                    session_token=opened.token,
                    idempotency_key="idem_sbp_t15",
                )
        self.assertEqual(ctx.exception.code, "SIGNER_ROLE_NOT_BOUND")
        self.assertIsNone(
            self.kernel.conn.execute(
                "SELECT proof_id FROM supplier_block_proofs WHERE booking_id = ?",
                (booking.booking_id,),
            ).fetchone()
        )

    def test_T16_a_session_revoked_mid_flight_cannot_confirm(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t16")
        proof = self.proof_for(booking)
        _, opened, interleave = self.revoke_between_checks(SIGNER_IDENTITY)
        with interleave, self.assertRaises(DomainError) as ctx:
            self.kernel.confirm_booking(
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_g2_t16",
                session_token=opened.token,
            )
        self.assertEqual(ctx.exception.code, "SESSION_REVOKED")
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")

    def test_T18_a_revoked_session_cannot_even_replay_a_confirmed_booking(self):
        """F-02 (Sonnet review): the replay read is inside the authorization boundary."""
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t18")
        proof = self.proof_for(booking)
        _, opened = self.open_session()
        self.kernel.confirm_booking(
            booking_id=booking.booking_id,
            block_proof_id=proof,
            command_id="cmd_g2_t18_first",
            session_token=opened.token,
        )
        self.assertEqual(self.status_of(booking), "CONFIRMED")
        _, opened, interleave = self.revoke_between_checks(SIGNER_IDENTITY)
        with interleave, self.assertRaises(DomainError) as ctx:
            self.kernel.confirm_booking(
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_g2_t18_replay",
                session_token=opened.token,
            )
        self.assertEqual(ctx.exception.code, "SESSION_REVOKED")


class TestAnotherAgencyHasNoAuthorityHere(LinearizationCase):
    """G2-T17 — F-01 (Sonnet review): a session minted in agency B proves nothing in A."""

    def other_agency_session(self):
        """Mint a real reservation_manager session for agency_b, on the same database."""
        import agency_roles as AR
        import totp as TT

        now = ID.to_iso(self.kernel.clock.now())
        self.peer.execute(
            "INSERT OR IGNORE INTO agencies (agency_id, name, status, created_at, updated_at)"
            " VALUES (?, 'Agency B (synthetic)', 'ACTIVE', ?, ?)",
            ("agency_b", now, now),
        )
        roles = AR.AgencyRoles(self.peer, self.kernel.clock, agency_id="agency_b")
        try:
            roles.seed_blueprint(actor="owner-1", actor_role="owner")
        except Exception:
            pass
        roles.assign_holder(
            "reservation_manager", holder_ref="rm-b", actor="owner-1", actor_role="owner"
        )
        secret_b = TT.generate_secret()
        svc = ID.SessionService(
            self.peer,
            clock=self.kernel.clock,
            ids=self.kernel.ids,
            agency_id="agency_b",
            secrets=ID.MappingSecretResolver({"rm-b-ref": secret_b}),
        )
        at_moment = self.kernel.clock.now() - ID.timedelta(seconds=120)
        base = ID.to_iso(at_moment)
        svc.enroll_totp("rm-b", secret_ref="rm-b-ref", at=base)
        svc.confirm_totp("rm-b", TT.totp(secret_b, at_moment), at=base)
        code = TT.totp(secret_b, self.kernel.clock.now())
        opened = svc.open_session("rm-b", "reservation_manager", mfa_code=code)
        svc.satisfy_step_up(opened.session.session_id, code)
        self.peer.commit()
        return opened.token

    def test_T17_a_foreign_agency_session_is_refused_on_every_privileged_path(self):
        self.flag(False)
        booking = self.paid_booking(key="idem_g2_t17")
        proof = self.proof_for(booking)
        token_b = self.other_agency_session()
        # The token exists — it must simply not resolve in agency A.
        with self.assertRaises(DomainError) as ctx:
            self.kernel.confirm_booking(
                booking_id=booking.booking_id,
                block_proof_id=proof,
                command_id="cmd_g2_t17_conf",
                session_token=token_b,
            )
        self.assertEqual(ctx.exception.code, "SESSION_NOT_FOUND")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.record_supplier_block_proof(
                booking_id=booking.booking_id,
                evidence_id="ev_t17",
                supplier_id="sup_1",
                relationship_id="rel_1",
                check_in=booking.check_in,
                check_out=booking.check_out,
                verification_method="MANAGER_CALL",
                verified_by="rm-b",
                actor="rm-b",
                verifier_role="reservation_manager",
                session_token=token_b,
                idempotency_key="idem_sbp_t17",
            )
        self.assertEqual(ctx.exception.code, "SESSION_NOT_FOUND")
        with self.assertRaises(DomainError) as ctx:
            self.kernel.revoke_supplier_block_proof(
                proof_id=proof,
                reason="foreign agency",
                actor="rm-b",
                actor_role="reservation_manager",
                session_token=token_b,
            )
        self.assertEqual(ctx.exception.code, "SESSION_NOT_FOUND")
        self.assertEqual(self.status_of(booking), "PAID_IN_FULL")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
