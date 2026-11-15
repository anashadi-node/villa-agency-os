"""Policy upah = data berversi, bukan angka di dalam kode (T2.1).

Empat aturan, dan alasan di baliknya:

1. **Tidak ada satu angka upah pun di modul ini.** Tarif dan satuan dibaca dari
   ``workforce_pay_rates``; aturan lembur, potongan, dan bonus dibaca dari
   ``workforce_policy_versions``. Menaruh tarif di kode berarti mengubah upah
   orang lewat deploy, bukan lewat persetujuan.
2. **Jendela berlaku setengah terbuka**, ``[effective_from, effective_to)``.
   Jendela tertutup membuat hari pergantian tarif punya dua kebenaran sekaligus;
   setengah terbuka memberi tepat satu tarif untuk satu tanggal.
3. **Dua versi sah untuk satu tanggal adalah kegagalan, bukan pilihan.**
   Memilih yang "paling baru" berarti diam-diam mengubah upah saat ada baris
   ganda; modul ini menolak (``..._AMBIGUOUS``) dan minta manusia merapikan
   datanya.
4. **Isi policy diperiksa terhadap checksum-nya.** Payload yang berubah tanpa
   versi baru adalah perubahan upah tanpa jejak persetujuan; itu ditolak.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional

from primitives import (
    DomainError,
    canonical_json,
    parse_iso_date,
    sha256_hex,
    to_iso_date,
)

POLICY_CODES = (
    "WORK_HOURS",
    "OVERTIME",
    "BREAK",
    "LEAVE",
    "PAYROLL_TAX",
    "PAYROLL_BPJS",
    "ATTENDANCE_GEOFENCE",
    "TASK_CAPACITY",
)

PAY_COMPONENTS = (
    "BASE_MONTHLY",
    "BASE_DAILY",
    "BASE_HOURLY",
    "OVERTIME_HOURLY",
    "MEAL_ALLOWANCE",
    "TRANSPORT_ALLOWANCE",
    "ATTENDANCE_BONUS",
)


class PayrollPolicyError(DomainError):
    """Kegagalan yang disengaja saat membaca policy upah.

    ``details`` diterima sebagai pemetaan supaya setiap kegagalan membawa bukti
    terstruktur (kode policy, versi, tanggal) yang bisa dipakai auditi log.
    """

    code = "WORKFORCE_PAYROLL_POLICY_ERROR"

    def __init__(self, message: str, details: Optional[Mapping[str, Any]] = None, *,
                 code: Optional[str] = None) -> None:
        super().__init__(message, code=code, **(dict(details) if details else {}))


class PolicyNotFound(PayrollPolicyError):
    code = "WORKFORCE_POLICY_NOT_FOUND"


class PolicyAmbiguous(PayrollPolicyError):
    code = "WORKFORCE_POLICY_AMBIGUOUS"


class PolicyChecksumMismatch(PayrollPolicyError):
    code = "WORKFORCE_POLICY_CHECKSUM_MISMATCH"


class PolicyPayloadInvalid(PayrollPolicyError):
    code = "WORKFORCE_POLICY_PAYLOAD_INVALID"


class PayRateNotFound(PayrollPolicyError):
    code = "WORKFORCE_PAY_RATE_NOT_FOUND"


class PayRateAmbiguous(PayrollPolicyError):
    code = "WORKFORCE_PAY_RATE_AMBIGUOUS"


class PayRateUnitMismatch(PayrollPolicyError):
    code = "WORKFORCE_PAY_RATE_UNIT_MISMATCH"


class PayRateCurrencyMismatch(PayrollPolicyError):
    code = "WORKFORCE_PAY_RATE_CURRENCY_MISMATCH"


@dataclass(frozen=True)
class PolicyVersion:
    """Satu versi policy yang berlaku pada satu tanggal."""

    policy_version_id: str
    agency_id: str
    policy_code: str
    version: int
    payload: Mapping[str, Any]
    checksum_sha256: str
    effective_from: date
    effective_to: Optional[date]

    def number(self, key: str, *, default: Optional[int] = None) -> int:
        """Ambil satu angka dari payload, tolak yang bukan bilangan bulat."""
        value = self.payload.get(key, default)
        if value is None:
            raise PolicyPayloadInvalid(
                f"policy {self.policy_code} v{self.version} does not carry '{key}'",
                {"policy_code": self.policy_code, "key": key},
            )
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyPayloadInvalid(
                f"policy {self.policy_code}: '{key}' must be an integer >= 0",
                {"policy_code": self.policy_code, "key": key, "value": value},
            )
        return value


@dataclass(frozen=True)
class PayRate:
    """Tarif satu komponen upah untuk satu orang pada satu tanggal."""

    pay_rate_id: str
    staff_id: str
    component_code: str
    unit: str
    amount_minor: int
    currency: str
    effective_from: date
    effective_to: Optional[date]
    policy_version_id: str


@dataclass(frozen=True)
class PolicyBundle:
    """Semua policy dan tarif yang berlaku untuk satu orang pada satu tanggal."""

    staff_id: str
    on_date: date
    policies: Mapping[str, PolicyVersion]
    rates: Mapping[str, PayRate]

    def policy(self, code: str) -> PolicyVersion:
        try:
            return self.policies[code]
        except KeyError:
            raise PolicyNotFound(
                f"policy '{code}' is not in force on {to_iso_date(self.on_date)}",
                {"policy_code": code, "on_date": to_iso_date(self.on_date)},
            ) from None

    def rate(self, component_code: str, *, unit: str) -> PayRate:
        try:
            rate = self.rates[component_code]
        except KeyError:
            raise PayRateNotFound(
                f"pay rate '{component_code}' is not in force on {to_iso_date(self.on_date)}",
                {"component_code": component_code, "on_date": to_iso_date(self.on_date)},
            ) from None
        if rate.unit != unit:
            raise PayRateUnitMismatch(
                f"tarif '{component_code}' bersatuan {rate.unit}, diharapkan {unit}",
                {"component_code": component_code, "unit": rate.unit, "expected": unit},
            )
        return rate

    @property
    def currency(self) -> str:
        currencies = {rate.currency for rate in self.rates.values()}
        if len(currencies) != 1:
            raise PayRateCurrencyMismatch(
                "one payslip can only carry one currency",
                {"currencies": sorted(currencies)},
            )
        return currencies.pop()


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _parse_payload(policy_code: str, raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise PolicyPayloadInvalid(
            f"payload policy {policy_code} bukan JSON",
            {"policy_code": policy_code, "reason": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise PolicyPayloadInvalid(
            f"policy payload {policy_code} must be a JSON object",
            {"policy_code": policy_code},
        )
    return payload


def _verify_checksum(policy_code: str, raw: str, payload: Mapping[str, Any],
                     stored: str) -> None:
    """Terima dua bentuk sidik jari yang sama-sama bisa diverifikasi.

    (a) sidik jari payload terkanonikalisasi — tahan terhadap perbedaan spasi dan
    urutan kunci, sehingga format ulang JSON bukan "tamper"; (b) sidik jari teks
    mentah — bentuk yang dipakai kalau penulis menyimpan apa adanya. Bentuk lain
    dianggap tidak sah: isi yang berubah tanpa versi baru harus ditolak.
    """
    canonical = sha256_hex(canonical_json(payload))
    literal = sha256_hex(raw)
    if stored not in (canonical, literal):
        raise PolicyChecksumMismatch(
            f"policy {policy_code} checksum does not match its content",
            {"policy_code": policy_code, "stored": stored[:12], "computed": canonical[:12]},
        )


def _load_policy_rows(conn: sqlite3.Connection, policy_code: str, agency_id: str,
                      on_date: date) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT policy_version_id, agency_id, policy_code, version, payload_json,"
        " checksum_sha256, effective_from, effective_to"
        " FROM workforce_policy_versions"
        " WHERE agency_id = ? AND policy_code = ?"
        "   AND effective_from <= ?"
        "   AND (effective_to IS NULL OR effective_to > ?)"
        " ORDER BY effective_from, version",
        (agency_id, policy_code, to_iso_date(on_date), to_iso_date(on_date)),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def load_policy_version(conn: sqlite3.Connection, policy_code: str, on_date: date, *,
                        agency_id: str = "agency_default") -> PolicyVersion:
    """Versi policy yang berlaku pada ``on_date``; nol atau dua versi = gagal."""
    rows = _load_policy_rows(conn, policy_code, agency_id, on_date)
    if not rows:
        raise PolicyNotFound(
            f"policy '{policy_code}' is not in force on {to_iso_date(on_date)}",
            {"policy_code": policy_code, "agency_id": agency_id,
             "on_date": to_iso_date(on_date)},
        )
    if len(rows) > 1:
        raise PolicyAmbiguous(
            f"policy '{policy_code}' has {len(rows)} versions in force on "
            f"{to_iso_date(on_date)}; effective windows must not overlap",
            {"policy_code": policy_code,
             "versions": [row["policy_version_id"] for row in rows]},
        )
    row = rows[0]
    payload = _parse_payload(policy_code, row["payload_json"])
    _verify_checksum(policy_code, row["payload_json"], payload, row["checksum_sha256"])
    return PolicyVersion(
        policy_version_id=str(row["policy_version_id"]),
        agency_id=str(row["agency_id"]),
        policy_code=str(row["policy_code"]),
        version=int(row["version"]),
        payload=payload,
        checksum_sha256=str(row["checksum_sha256"]),
        effective_from=parse_iso_date(row["effective_from"]),
        effective_to=parse_iso_date(row["effective_to"]) if row["effective_to"] else None,
    )


def load_pay_rates(conn: sqlite3.Connection, staff_id: str, on_date: date, *,
                   agency_id: str = "agency_default") -> dict[str, PayRate]:
    """Tarif semua komponen yang berlaku pada ``on_date``, satu per komponen."""
    rows = conn.execute(
        "SELECT pay_rate_id, staff_id, component_code, unit, amount_minor, currency,"
        " effective_from, effective_to, policy_version_id"
        " FROM workforce_pay_rates"
        " WHERE agency_id = ? AND staff_id = ?"
        "   AND effective_from <= ?"
        "   AND (effective_to IS NULL OR effective_to > ?)"
        " ORDER BY component_code, effective_from",
        (agency_id, staff_id, to_iso_date(on_date), to_iso_date(on_date)),
    ).fetchall()
    rates: dict[str, PayRate] = {}
    for raw in rows:
        row = _row_dict(raw)
        component = str(row["component_code"])
        if component in rates:
            raise PayRateAmbiguous(
                f"pay rate '{component}' has more than one row in force on "
                f"{to_iso_date(on_date)}",
                {"component_code": component,
                 "pay_rate_ids": [rates[component].pay_rate_id, str(row["pay_rate_id"])]},
            )
        rates[component] = PayRate(
            pay_rate_id=str(row["pay_rate_id"]),
            staff_id=str(row["staff_id"]),
            component_code=component,
            unit=str(row["unit"]),
            amount_minor=int(row["amount_minor"]),
            currency=str(row["currency"]),
            effective_from=parse_iso_date(row["effective_from"]),
            effective_to=parse_iso_date(row["effective_to"]) if row["effective_to"] else None,
            policy_version_id=str(row["policy_version_id"]),
        )
    return rates


def load_bundle(conn: sqlite3.Connection, staff_id: str, on_date: date, *,
                agency_id: str = "agency_default",
                policy_codes: tuple[str, ...] = POLICY_CODES) -> PolicyBundle:
    """Satu paket baca untuk satu orang: seluruh policy + seluruh tarif.

    Policy yang tidak berlaku **tidak** langsung menggagalkan paket — penggajian
    hanya butuh yang memang dipakai. Tarif kosong menggagalkan: orang tanpa tarif
    yang dihitung sebagai nol adalah slip gaji palsu.
    """
    policies: dict[str, PolicyVersion] = {}
    for code in policy_codes:
        try:
            policies[code] = load_policy_version(conn, code, on_date, agency_id=agency_id)
        except PolicyNotFound:
            # Policy yang tidak berlaku bukan alasan menggagalkan paket: penggajian
            # hanya butuh yang dipakai, dan ``bundle.policy()`` tetap gagal keras
            # ketika ada yang benar-benar diminta tetapi tidak ada.
            continue
    rates = load_pay_rates(conn, staff_id, on_date, agency_id=agency_id)
    if not rates:
        raise PayRateNotFound(
            f"no pay rate is in force for {staff_id} on {to_iso_date(on_date)}",
            {"staff_id": staff_id, "on_date": to_iso_date(on_date)},
        )
    return PolicyBundle(staff_id=staff_id, on_date=on_date, policies=policies, rates=rates)
