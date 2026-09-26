"""Identity: sessions, TOTP enrolment, and step-up (ADR-0005, accepted).

The rules this module exists to enforce, and why each one is here:

1. **MFA is mandatory for OWNER and FINANCE.** A password alone must not open a
   session that can move money, so :meth:`SessionService.open_session` refuses
   those roles without a valid TOTP code instead of trusting the caller to ask.
2. **A session has two deadlines.** Idle *and* absolute. One deadline alone is
   defeated either by an attacker who waits, or by a tab left open all night.
3. **High-risk actions need a fresh factor.** A session that satisfied MFA in the
   morning must not authorise a bank change in the evening: money/bank actions
   require a step-up inside :data:`STEP_UP_WINDOW`.
4. **The database never holds the shared TOTP secret.** A factor stores a
   ``secret_ref`` resolved through a :class:`SecretResolver`, so leaking the
   ``mfa_factors`` table does not by itself hand over the second factor.
5. **Every auth act is evidence.** Logins, refusals, replays and step-ups are
   appended to ``auth_events`` (append-only at the database level).

Scope: every query is scoped by ``agency_id``. An identity belongs to exactly one
agency, and a manager in one agency cannot be an approver in another.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Protocol

import totp as T
from primitives import (
    DEFAULT_AGENCY_ID,
    DomainError,
    IdFactory,
    SystemClock,
    UTC,
    canonical_json,
    parse_iso,
    require_ref,
    require_text,
    sha256_hex,
    to_iso,
)

__all__ = [
    "HIGH_RISK_SUBJECT_TYPES",
    "MFA_REQUIRED_ROLES",
    "MFARequired",
    "SESSION_ABSOLUTE_TIMEOUT",
    "SESSION_IDLE_TIMEOUT",
    "STEP_UP_WINDOW",
    "EnvSecretResolver",
    "IdentityError",
    "InvalidCode",
    "LoginResult",
    "MappingSecretResolver",
    "RevokedSession",
    "SecretResolver",
    "Session",
    "SessionExpired",
    "SessionNotFound",
    "SessionService",
    "StepUpRequired",
]

#: Roles that may not hold a session before the second factor is satisfied.
MFA_REQUIRED_ROLES: tuple[str, ...] = ("owner", "finance")

#: Actions whose approval request needs a *fresh* factor (step-up), not merely a
#: session opened earlier. Bank details and money leaving the account are the two
#: things an attacker with a stolen session wants most.
HIGH_RISK_SUBJECT_TYPES: tuple[str, ...] = ("refund", "payout", "bank_change")

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=12)

#: How long one step-up satisfies a high-risk action.
STEP_UP_WINDOW = timedelta(minutes=10)

TOKEN_BYTES = 32


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class IdentityError(DomainError):
    """Base class for deliberate refusals from this module."""


class SessionNotFound(IdentityError):
    code = "SESSION_NOT_FOUND"


class SessionExpired(IdentityError):
    code = "SESSION_EXPIRED"


class RevokedSession(IdentityError):
    code = "SESSION_REVOKED"


class MFARequired(IdentityError):
    code = "MFA_REQUIRED"


class InvalidCode(IdentityError):
    code = "MFA_CODE_INVALID"


class StepUpRequired(IdentityError):
    code = "STEP_UP_REQUIRED"


class UnknownFactor(IdentityError):
    code = "MFA_FACTOR_UNKNOWN"


# --------------------------------------------------------------------------- #
# Secret resolution: the shared secret never lives in these tables
# --------------------------------------------------------------------------- #
class SecretResolver(Protocol):
    """Where a factor's shared secret actually comes from."""

    def resolve(self, secret_ref: str) -> str:  # pragma: no cover - protocol
        ...


class MappingSecretResolver:
    """A resolver backed by a mapping (tests, single-process pilot)."""

    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    def resolve(self, secret_ref: str) -> str:
        try:
            return self._secrets[secret_ref]
        except KeyError as exc:
            raise UnknownFactor(
                f"no secret registered for {secret_ref!r}", secret_ref=secret_ref
            ) from exc


class EnvSecretResolver:
    """Read ``<prefix><normalised secret_ref>`` from the environment.

    The pilot's zero-dependency option: the secret lives in the process
    environment (or a systemd ``EnvironmentFile`` with mode 0600), never in the
    database. A production deployment swaps this for Vault/KMS by implementing
    the same one-method protocol.
    """

    def __init__(self, prefix: str = "VILLA_TOTP_SECRET_", environment: Optional[Mapping[str, str]] = None) -> None:
        self._prefix = prefix
        self._env = environment

    def resolve(self, secret_ref: str) -> str:
        source = os.environ if self._env is None else self._env
        name = self._prefix + secret_ref.upper().replace("-", "_").replace(".", "_")
        value = source.get(name)
        if not value:
            raise UnknownFactor(
                f"no TOTP secret in the environment for {secret_ref!r}",
                secret_ref=secret_ref,
                expected_variable=name,
            )
        return value


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Session:
    """One logged-in caller, with both clocks and its factor state."""

    session_id: str
    agency_id: str
    identity: str
    role: str
    issued_at: str
    last_seen_at: str
    idle_expires_at: str
    expires_at: str
    mfa_satisfied_at: Optional[str]
    step_up_until: Optional[str]
    revoked_at: Optional[str]
    revoked_reason: Optional[str]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Session":
        return cls(
            session_id=str(row["session_id"]),
            agency_id=str(row["agency_id"]),
            identity=str(row["identity"]),
            role=str(row["role"]),
            issued_at=str(row["issued_at"]),
            last_seen_at=str(row["last_seen_at"]),
            idle_expires_at=str(row["idle_expires_at"]),
            expires_at=str(row["expires_at"]),
            mfa_satisfied_at=None if row["mfa_satisfied_at"] is None else str(row["mfa_satisfied_at"]),
            step_up_until=None if row["step_up_until"] is None else str(row["step_up_until"]),
            revoked_at=None if row["revoked_at"] is None else str(row["revoked_at"]),
            revoked_reason=None if row["revoked_reason"] is None else str(row["revoked_reason"]),
        )

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_absolute_expired(self, at: datetime) -> bool:
        return parse_iso(self.expires_at) <= at

    def is_idle_expired(self, at: datetime) -> bool:
        return parse_iso(self.idle_expires_at) <= at

    def needs_mfa(self, required_roles: tuple[str, ...] = MFA_REQUIRED_ROLES) -> bool:
        """True when this session's role owes a second factor and has not paid it.

        ``required_roles`` comes from the §16 policy store when the service was
        built with one; the module constant is the documented baseline.
        """
        return self.role in required_roles and self.mfa_satisfied_at is None

    def has_step_up(self, at: datetime) -> bool:
        return self.step_up_until is not None and parse_iso(self.step_up_until) > at


@dataclass(frozen=True)
class LoginResult:
    """The session plus the **raw** token — shown once, never stored."""

    session: Session
    token: str


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class SessionService:
    """Sessions, TOTP factors and step-up over one SQLite/PostgreSQL connection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Any = None,
        ids: Optional[IdFactory] = None,
        *,
        agency_id: str = DEFAULT_AGENCY_ID,
        secrets: Optional[SecretResolver] = None,
        idle_timeout: timedelta = SESSION_IDLE_TIMEOUT,
        absolute_timeout: timedelta = SESSION_ABSOLUTE_TIMEOUT,
        step_up_window: timedelta = STEP_UP_WINDOW,
        mfa_required_roles: tuple[str, ...] = MFA_REQUIRED_ROLES,
        high_risk_subject_types: tuple[str, ...] = HIGH_RISK_SUBJECT_TYPES,
    ) -> None:
        self.conn = conn
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.clock = clock or SystemClock()
        self.ids = ids or IdFactory()
        self.agency_id = require_ref(agency_id, "agency_id")
        self.secrets = secrets if secrets is not None else EnvSecretResolver()
        if idle_timeout <= timedelta(0) or absolute_timeout <= timedelta(0):
            raise IdentityError("timeouts must be positive", code="INVALID_TIMEOUT")
        if idle_timeout > absolute_timeout:
            raise IdentityError(
                "idle timeout must not exceed the absolute timeout",
                code="INVALID_TIMEOUT",
                idle_seconds=int(idle_timeout.total_seconds()),
                absolute_seconds=int(absolute_timeout.total_seconds()),
            )
        self.idle_timeout = idle_timeout
        self.absolute_timeout = absolute_timeout
        self.step_up_window = step_up_window
        self.mfa_required_roles = tuple(mfa_required_roles)
        self.high_risk_subject_types = tuple(high_risk_subject_types)

    @classmethod
    def from_policy(
        cls,
        conn: sqlite3.Connection,
        policy: Any,
        *,
        ids: Optional[IdFactory] = None,
        secrets: Optional[SecretResolver] = None,
        at: Optional[Any] = None,
    ) -> "SessionService":
        """Build a service whose security numbers come from the §16 policy store.

        §16 keeps these numbers out of the source. The resolver is the authority
        for the session timeouts, the step-up window, the MFA role set and the
        high-risk subject set; the module constants stay as the documented
        baseline, and ``tests/test_wave1h_policy_engine.py`` fails if a constant
        and its published revision drift apart.
        """
        return cls(
            conn,
            clock=policy.clock,
            ids=ids,
            agency_id=policy.agency_id,
            secrets=secrets,
            idle_timeout=timedelta(
                minutes=policy.resolve("security.session.idle_timeout_minutes", at=at).as_int()
            ),
            absolute_timeout=timedelta(
                hours=policy.resolve("security.session.absolute_timeout_hours", at=at).as_int()
            ),
            step_up_window=timedelta(
                minutes=policy.resolve("security.step_up.window_minutes", at=at).as_int()
            ),
            mfa_required_roles=tuple(
                policy.resolve("security.mfa.required_roles", at=at).as_roles()
            ),
            high_risk_subject_types=tuple(
                policy.resolve("security.step_up.high_risk_subjects", at=at).as_json()
            ),
        )

    # -- plumbing ---------------------------------------------------------- #
    def _now(self) -> datetime:
        return self.clock.now().astimezone(UTC)

    def _stamp(self, at: Optional[Any] = None) -> str:
        return to_iso(self._now() if at is None else self._moment(at))

    @staticmethod
    def _moment(at: Any) -> datetime:
        if isinstance(at, str):
            return parse_iso(at)
        if isinstance(at, datetime):
            if at.tzinfo is None:
                raise IdentityError("at must carry a timezone", code="INVALID_TIMESTAMP")
            return at.astimezone(UTC)
        raise IdentityError("at must be an ISO-8601 string or datetime", code="INVALID_TIMESTAMP")

    def _event(self, *, kind: str, identity: str, actor: str, session_id: Optional[str] = None, detail: Optional[Mapping[str, Any]] = None, at: Optional[Any] = None) -> None:
        self.conn.execute(
            "INSERT INTO auth_events (auth_event_id, agency_id, identity, kind, session_id,"
            " actor, detail, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.ids.new("auth"),
                self.agency_id,
                identity,
                kind,
                session_id,
                actor,
                canonical_json(dict(detail or {})),
                self._stamp(at),
            ),
        )

    def _session_row(self, session_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM auth_sessions WHERE session_id = ? AND agency_id = ?",
            (require_ref(session_id, "session_id"), self.agency_id),
        ).fetchone()
        if row is None:
            raise SessionNotFound(f"session tidak ditemukan: {session_id}", session_id=session_id)
        return row

    # -- factors ----------------------------------------------------------- #
    def enroll_totp(
        self,
        identity: str,
        *,
        secret_ref: str,
        digits: int = T.DEFAULT_DIGITS,
        step_seconds: int = T.DEFAULT_STEP_SECONDS,
        algorithm: str = T.DEFAULT_ALGORITHM,
        at: Optional[Any] = None,
    ) -> str:
        """Register a factor in ``DISABLED`` state; a real code activates it."""
        who = require_ref(identity, "identity")
        ref = require_text(secret_ref, "secret_ref", max_len=200)
        if digits not in (6, 8):
            raise IdentityError("digits must be 6 or 8", code="INVALID_FACTOR", digits=digits)
        if not 15 <= int(step_seconds) <= 120:
            raise IdentityError("step_seconds out of range", code="INVALID_FACTOR", step_seconds=step_seconds)
        if algorithm not in ("sha1", "sha256", "sha512"):
            raise IdentityError("unknown algorithm", code="INVALID_FACTOR", algorithm=algorithm)
        stamp = self._stamp(at)
        existing = self.conn.execute(
            "SELECT factor_id FROM mfa_factors WHERE agency_id = ? AND identity = ? AND kind = 'TOTP'",
            (self.agency_id, who),
        ).fetchone()
        if existing is not None:
            raise IdentityError(
                "faktor TOTP untuk identitas ini sudah ada",
                code="MFA_FACTOR_EXISTS",
                identity=who,
                factor_id=str(existing["factor_id"]),
            )
        factor_id = self.ids.new("mfa")
        self.conn.execute(
            "INSERT INTO mfa_factors (factor_id, agency_id, identity, kind, secret_ref, digits,"
            " step_seconds, algorithm, status, last_used_step, confirmed_at, created_at, updated_at)"
            " VALUES (?, ?, ?, 'TOTP', ?, ?, ?, ?, 'DISABLED', NULL, NULL, ?, ?)",
            (factor_id, self.agency_id, who, ref, int(digits), int(step_seconds), algorithm, stamp, stamp),
        )
        self._event(kind="MFA_ENROLLED", identity=who, actor=who, detail={"factor_id": factor_id, "secret_ref": ref}, at=at)
        return factor_id

    def _factor_row(self, identity: str) -> sqlite3.Row:
        who = require_ref(identity, "identity")
        row = self.conn.execute(
            "SELECT * FROM mfa_factors WHERE agency_id = ? AND identity = ? AND kind = 'TOTP'",
            (self.agency_id, who),
        ).fetchone()
        if row is None:
            raise UnknownFactor(
                "identitas ini belum punya faktor TOTP", code="MFA_FACTOR_UNKNOWN", identity=who
            )
        return row

    def verify_factor_code(self, identity: str, code: Any, *, at: Optional[Any] = None) -> int:
        """Check a code against the stored factor, refusing a spent step."""
        row = self._factor_row(identity)
        if str(row["status"]) != "ACTIVE":
            raise InvalidCode(
                "faktor belum aktif (belum dikonfirmasi)",
                code="MFA_FACTOR_INACTIVE",
                identity=identity,
                status=str(row["status"]),
            )
        moment = self._now() if at is None else self._moment(at)
        secret = self.secrets.resolve(str(row["secret_ref"]))
        result = T.verify(
            secret,
            code,
            moment,
            step_seconds=int(row["step_seconds"]),
            digits=int(row["digits"]),
            algorithm=str(row["algorithm"]),
            last_used_step=None if row["last_used_step"] is None else int(row["last_used_step"]),
        )
        who = str(row["identity"])
        if result.ok:
            self.conn.execute(
                "UPDATE mfa_factors SET last_used_step = ?, updated_at = ? WHERE factor_id = ? AND agency_id = ?",
                (int(result.step), to_iso(moment), str(row["factor_id"]), self.agency_id),
            )
            self._event(kind="MFA_SUCCEEDED", identity=who, actor=who, detail={"step": result.step}, at=at)
            return int(result.step)
        if result.replayed:
            self._event(kind="MFA_REPLAY_REFUSED", identity=who, actor=who, detail={"step": result.step}, at=at)
            raise InvalidCode(
                "kode ini sudah dipakai; tunggu kode berikutnya",
                code="MFA_CODE_REPLAYED",
                identity=who,
            )
        self._event(kind="MFA_FAILED", identity=who, actor=who, detail={"reason": result.reason}, at=at)
        raise InvalidCode("kode TOTP tidak cocok", code="MFA_CODE_INVALID", identity=who, reason=result.reason)

    def confirm_totp(self, identity: str, code: Any, *, at: Optional[Any] = None) -> str:
        """Activate a factor once the holder proves they can generate a code."""
        row = self._factor_row(identity)
        moment = self._now() if at is None else self._moment(at)
        secret = self.secrets.resolve(str(row["secret_ref"]))
        result = T.verify(
            secret,
            code,
            moment,
            step_seconds=int(row["step_seconds"]),
            digits=int(row["digits"]),
            algorithm=str(row["algorithm"]),
            last_used_step=None if row["last_used_step"] is None else int(row["last_used_step"]),
        )
        if not result.ok:
            self._event(
                kind="MFA_FAILED",
                identity=str(row["identity"]),
                actor=str(row["identity"]),
                detail={"reason": result.reason, "phase": "confirm"},
                at=at,
            )
            raise InvalidCode(
                "kode konfirmasi tidak cocok", code="MFA_CODE_INVALID", identity=identity, reason=result.reason
            )
        stamp = to_iso(moment)
        self.conn.execute(
            "UPDATE mfa_factors SET status = 'ACTIVE', confirmed_at = ?, last_used_step = ?,"
            " updated_at = ? WHERE factor_id = ? AND agency_id = ?",
            (stamp, int(result.step), stamp, str(row["factor_id"]), self.agency_id),
        )
        return str(row["factor_id"])

    # -- sessions ---------------------------------------------------------- #
    def open_session(
        self,
        identity: str,
        role: str,
        *,
        mfa_code: Any = None,
        at: Optional[Any] = None,
    ) -> LoginResult:
        """Open a session. Roles in :data:`MFA_REQUIRED_ROLES` must bring a code.

        The refusal happens *before* any row is written: a session that cannot
        satisfy the second factor must not exist, even briefly.
        """
        who = require_ref(identity, "identity")
        role_name = require_ref(role, "role", max_len=64)
        moment = self._now() if at is None else self._moment(at)
        mfa_satisfied_at: Optional[str] = None
        if role_name in self.mfa_required_roles:
            if mfa_code is None:
                self._event(kind="ACCESS_DENIED", identity=who, actor=who,
                            detail={"reason": "MFA_REQUIRED", "role": role_name}, at=at)
                raise MFARequired(
                    "peran ini wajib MFA: kirim kode TOTP untuk membuka sesi",
                    code="MFA_REQUIRED",
                    identity=who,
                    role=role_name,
                )
            self.verify_factor_code(who, mfa_code, at=at)
            mfa_satisfied_at = to_iso(moment)

        # token_hex, not token_urlsafe: urlsafe base64 can start with "-" or "_",
        # which the platform's own opaque-reference rule (`primitives._REF_TOKEN`,
        # first character must be alphanumeric) rejects. A token that the system
        # refuses to accept 3% of the time is a login that fails at random, so the
        # generator must satisfy the validator by construction — 256 bits either way.
        token = secrets.token_hex(TOKEN_BYTES)
        session_id = self.ids.new("sess")
        issued = to_iso(moment)
        idle_until = to_iso(moment + self.idle_timeout)
        absolute_until = to_iso(moment + self.absolute_timeout)
        self.conn.execute(
            "INSERT INTO auth_sessions (session_id, agency_id, identity, role, token_sha256,"
            " issued_at, last_seen_at, idle_expires_at, expires_at, mfa_satisfied_at,"
            " step_up_until, revoked_at, revoked_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
            (session_id, self.agency_id, who, role_name, sha256_hex(token), issued, issued,
             idle_until, absolute_until, mfa_satisfied_at),
        )
        self._event(kind="LOGIN", identity=who, actor=who, session_id=session_id,
                    detail={"role": role_name, "mfa": mfa_satisfied_at is not None}, at=at)
        session = Session.from_row(self._session_row(session_id))
        return LoginResult(session=session, token=token)

    def authenticate(self, token: Any, *, at: Optional[Any] = None, touch: bool = True) -> Session:
        """Resolve a raw token to a live session, or refuse it."""
        raw = require_ref(token, "token", max_len=512)
        moment = self._now() if at is None else self._moment(at)
        row = self.conn.execute(
            "SELECT * FROM auth_sessions WHERE token_sha256 = ? AND agency_id = ?",
            (sha256_hex(raw), self.agency_id),
        ).fetchone()
        if row is None:
            raise SessionNotFound("token tidak dikenali", code="SESSION_NOT_FOUND")
        session = Session.from_row(row)
        if session.is_revoked:
            self._event(kind="SESSION_REFUSED", identity=session.identity, actor=session.identity,
                        session_id=session.session_id, detail={"reason": "REVOKED"}, at=at)
            raise RevokedSession(
                "sesi ini sudah dicabut", code="SESSION_REVOKED", session_id=session.session_id
            )
        if session.is_absolute_expired(moment) or session.is_idle_expired(moment):
            self._event(kind="SESSION_REFUSED", identity=session.identity, actor=session.identity,
                        session_id=session.session_id, detail={"reason": "EXPIRED"}, at=at)
            raise SessionExpired("sesi sudah kedaluwarsa", code="SESSION_EXPIRED",
                                 session_id=session.session_id)
        if session.needs_mfa(self.mfa_required_roles):
            raise MFARequired("sesi ini belum memenuhi MFA", code="MFA_REQUIRED",
                              session_id=session.session_id)
        if touch:
            idle_until = to_iso(moment + self.idle_timeout)
            # The idle deadline never moves past the absolute one.
            if parse_iso(idle_until) > parse_iso(session.expires_at):
                idle_until = session.expires_at
            self.conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ?, idle_expires_at = ?"
                " WHERE session_id = ? AND agency_id = ?",
                (to_iso(moment), idle_until, session.session_id, self.agency_id),
            )
            session = Session.from_row(self._session_row(session.session_id))
        return session

    def rotate(self, token: Any, *, at: Optional[Any] = None) -> LoginResult:
        """Replace a token: the old one stops working in the same transaction."""
        session = self.authenticate(token, at=at, touch=False)
        moment = self._now() if at is None else self._moment(at)
        new_token = secrets.token_urlsafe(TOKEN_BYTES)
        self.conn.execute(
            "UPDATE auth_sessions SET token_sha256 = ?, last_seen_at = ?, idle_expires_at = ?"
            " WHERE session_id = ? AND agency_id = ?",
            (sha256_hex(new_token), to_iso(moment), to_iso(moment + self.idle_timeout),
             session.session_id, self.agency_id),
        )
        self._event(kind="SESSION_ROTATED", identity=session.identity, actor=session.identity,
                    session_id=session.session_id, at=at)
        return LoginResult(session=Session.from_row(self._session_row(session.session_id)), token=new_token)

    def revoke(self, session_id: str, *, reason: str, at: Optional[Any] = None) -> Session:
        row = self._session_row(session_id)
        note = require_text(reason, "reason", max_len=200)
        if len(note) < 3:
            raise IdentityError("reason must be at least 3 characters", code="INVALID_REASON")
        moment = self._now() if at is None else self._moment(at)
        self.conn.execute(
            "UPDATE auth_sessions SET revoked_at = ?, revoked_reason = ?"
            " WHERE session_id = ? AND agency_id = ? AND revoked_at IS NULL",
            (to_iso(moment), note, str(row["session_id"]), self.agency_id),
        )
        self._event(kind="SESSION_REVOKED", identity=str(row["identity"]), actor=str(row["identity"]),
                    session_id=str(row["session_id"]), detail={"reason": note}, at=at)
        return Session.from_row(self._session_row(str(row["session_id"])))

    def sweep_expired(self, *, at: Optional[Any] = None) -> tuple[str, ...]:
        """Revoke every session past either deadline; returns their ids."""
        moment = self._now() if at is None else self._moment(at)
        stamp = to_iso(moment)
        rows = self.conn.execute(
            "SELECT session_id FROM auth_sessions WHERE agency_id = ? AND revoked_at IS NULL"
            " AND (expires_at <= ? OR idle_expires_at <= ?) ORDER BY session_id",
            (self.agency_id, stamp, stamp),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE auth_sessions SET revoked_at = ?, revoked_reason = 'kedaluwarsa'"
                " WHERE session_id = ? AND agency_id = ?",
                (stamp, str(row["session_id"]), self.agency_id),
            )
        return tuple(str(row["session_id"]) for row in rows)

    # -- step-up ----------------------------------------------------------- #
    def satisfy_step_up(self, session_id: str, code: Any, *, at: Optional[Any] = None) -> Session:
        """Spend a fresh code to unlock high-risk actions for a short window."""
        row = self._session_row(session_id)
        session = Session.from_row(row)
        if session.is_revoked:
            raise RevokedSession("sesi ini sudah dicabut", code="SESSION_REVOKED",
                                 session_id=session.session_id)
        before = str(row["step_up_until"]) if row["step_up_until"] is not None else None
        self.verify_factor_code(session.identity, code, at=at)
        moment = self._now() if at is None else self._moment(at)
        self.conn.execute(
            "UPDATE auth_sessions SET mfa_satisfied_at = COALESCE(mfa_satisfied_at, ?),"
            " step_up_until = ? WHERE session_id = ? AND agency_id = ?",
            (to_iso(moment), to_iso(moment + self.step_up_window), session.session_id, self.agency_id),
        )
        self._event(kind="STEP_UP_GRANTED", identity=session.identity, actor=session.identity,
                    session_id=session.session_id, detail={"before": before}, at=at)
        return Session.from_row(self._session_row(session.session_id))

    def require_step_up(self, session: Session, *, subject_type: str, at: Optional[Any] = None) -> None:
        """Refuse a high-risk action unless a factor was satisfied *recently*."""
        moment = self._now() if at is None else self._moment(at)
        if session.needs_mfa(self.mfa_required_roles):
            raise MFARequired("sesi ini belum memenuhi MFA", code="MFA_REQUIRED",
                              session_id=session.session_id)
        if subject_type not in self.high_risk_subject_types:
            return
        if session.has_step_up(moment):
            return
        self._event(kind="STEP_UP_REFUSED", identity=session.identity, actor=session.identity,
                    session_id=session.session_id, detail={"subject_type": subject_type}, at=at)
        raise StepUpRequired(
            "aksi berisiko tinggi butuh MFA ulang (step-up)",
            code="STEP_UP_REQUIRED",
            session_id=session.session_id,
            subject_type=subject_type,
            window_seconds=int(self.step_up_window.total_seconds()),
        )

    # -- reads ------------------------------------------------------------- #
    def events(self, *, identity: Optional[str] = None) -> tuple[Mapping[str, Any], ...]:
        """The append-only auth trail, oldest first."""
        if identity is None:
            rows = self.conn.execute(
                "SELECT * FROM auth_events WHERE agency_id = ? ORDER BY occurred_at, auth_event_id",
                (self.agency_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM auth_events WHERE agency_id = ? AND identity = ?"
                " ORDER BY occurred_at, auth_event_id",
                (self.agency_id, require_ref(identity, "identity")),
            ).fetchall()
        return tuple(dict(row) for row in rows)
