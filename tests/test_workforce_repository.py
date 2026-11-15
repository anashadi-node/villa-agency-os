"""Jalur tulis Workforce — satu transaksi, satu aturan idempotensi (T1.1).

Yang dikunci di sini bukan "kode bisa insert", tetapi empat janji yang, kalau
bocor, menghasilkan data ganda di buku kerja nyata:

* baris yang sama ditulis ulang tidak menambah baris;
* nama tabel/kolom dari pemanggil tidak pernah menjadi string SQL;
* kunci idempotensi tidak boleh berupa singkatan;
* kesalahan di tengah transaksi tidak meninggalkan baris separuh jadi.
"""

from __future__ import annotations

import pytest

from primitives import DomainError
from workforce.repository import (
    MIN_IDEMPOTENCY_KEY_LENGTH,
    BadIdentifier,
    InvalidIdempotencyKey,
    insert_once,
    require_idempotency_key,
    write_transaction,
)

AGENCY = "agency_default"


@pytest.fixture()
def probe(conn):
    """Tabel uji kecil dengan kunci unik yang meniru pola tabel Workforce."""
    conn.execute(
        "CREATE TABLE probe_rows ("
        " probe_id TEXT PRIMARY KEY,"
        " agency_id TEXT NOT NULL,"
        " idempotency_key TEXT NOT NULL,"
        " note TEXT,"
        " UNIQUE (agency_id, idempotency_key))"
    )
    return conn


def _row(probe_id: str = "probe_1", key: str = "idem-probe-1", note: str = "first") -> dict:
    return {
        "probe_id": probe_id,
        "agency_id": AGENCY,
        "idempotency_key": key,
        "note": note,
    }


def test_insert_once_writes_exactly_one_row(probe):
    assert insert_once(probe, table="probe_rows", columns=_row(),
                       conflict_on=("agency_id", "idempotency_key")) is True
    assert probe.execute("SELECT COUNT(*) FROM probe_rows").fetchone()[0] == 1


def test_insert_once_returns_false_on_replay_without_touching_the_row(probe):
    assert insert_once(probe, table="probe_rows", columns=_row(),
                       conflict_on=("agency_id", "idempotency_key")) is True
    replay = _row(note="second payload, same key")

    assert insert_once(probe, table="probe_rows", columns=replay,
                       conflict_on=("agency_id", "idempotency_key")) is False
    assert probe.execute("SELECT COUNT(*) FROM probe_rows").fetchone()[0] == 1
    assert probe.execute("SELECT note FROM probe_rows").fetchone()[0] == "first"


def test_insert_once_writes_a_new_row_for_a_new_key(probe):
    insert_once(probe, table="probe_rows", columns=_row(), conflict_on=("agency_id", "idempotency_key"))
    assert insert_once(probe, table="probe_rows", columns=_row(probe_id="probe_2", key="idem-probe-2"),
                       conflict_on=("agency_id", "idempotency_key")) is True
    assert probe.execute("SELECT COUNT(*) FROM probe_rows").fetchone()[0] == 2


def test_insert_once_rejects_a_table_name_that_is_not_a_plain_identifier(probe):
    with pytest.raises(BadIdentifier) as caught:
        insert_once(probe, table="probe_rows; DROP TABLE probe_rows", columns=_row(),
                    conflict_on=("agency_id",))
    assert caught.value.code == "WORKFORCE_BAD_IDENTIFIER"


def test_insert_once_rejects_a_column_name_that_is_not_a_plain_identifier(probe):
    with pytest.raises(BadIdentifier):
        insert_once(probe, table="probe_rows", columns={"note) VALUES (1); --": "x"},
                    conflict_on=("note) VALUES (1); --",))


def test_insert_once_rejects_conflict_key_outside_the_inserted_columns(probe):
    with pytest.raises(BadIdentifier) as caught:
        insert_once(probe, table="probe_rows", columns=_row(),
                    conflict_on=("agency_id", "not_a_column"))
    assert caught.value.details["missing"] == "not_a_column"


def test_require_idempotency_key_rejects_short_keys():
    with pytest.raises(InvalidIdempotencyKey) as caught:
        require_idempotency_key("a1")
    assert caught.value.code == "WORKFORCE_INVALID_IDEMPOTENCY_KEY"
    assert caught.value.details["length"] == 2


def test_require_idempotency_key_accepts_a_key_of_exactly_the_minimum_length():
    key = "k" * MIN_IDEMPOTENCY_KEY_LENGTH
    assert require_idempotency_key(key) == key


def test_write_transaction_commits_when_the_block_succeeds(probe):
    with write_transaction(probe):
        insert_once(probe, table="probe_rows", columns=_row(), conflict_on=("agency_id", "idempotency_key"))
    assert probe.execute("SELECT COUNT(*) FROM probe_rows").fetchone()[0] == 1


def test_write_transaction_rolls_back_everything_when_the_block_raises(probe):
    with pytest.raises(DomainError):
        with write_transaction(probe):
            insert_once(probe, table="probe_rows", columns=_row(), conflict_on=("agency_id", "idempotency_key"))
            raise DomainError("validator said no", code="REFUSED")

    assert probe.execute("SELECT COUNT(*) FROM probe_rows").fetchone()[0] == 0
