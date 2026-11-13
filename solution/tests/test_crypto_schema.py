import os
import sqlite3
from pathlib import Path

import pytest

from solution.crypto import Vault

SCHEMA = (Path(__file__).parent.parent / "schema.sql").read_text(encoding="utf-8")


def make_vault():
    return Vault(os.urandom(32), os.urandom(32))


def test_encrypt_roundtrip_and_randomness():
    v = make_vault()
    a, b = v.encrypt("+919876543210"), v.encrypt("+919876543210")
    assert a != b                                  # random nonce
    assert b"9876543210" not in a                  # no plaintext inside
    assert v.decrypt(a) == "+919876543210"


def test_blind_index_is_deterministic_and_keyed():
    v1, v2 = make_vault(), make_vault()
    assert v1.blind_index("+919876543210") == v1.blind_index("+919876543210")
    assert v1.blind_index("+919876543210") != v2.blind_index("+919876543210")


def test_same_key_twice_is_refused():
    k = os.urandom(32)
    with pytest.raises(ValueError):
        Vault(k, k)


def test_ledger_is_append_only():
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    con.execute("INSERT INTO events (event_id, event_type, occurred_at, payload_json, source_file, source_line)"
                " VALUES ('evt_1', 'call.initiated', '2026-10-01T00:00:00+00:00', '{}', 'f', 1)")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("UPDATE events SET event_type = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute("DELETE FROM events")
    with pytest.raises(sqlite3.IntegrityError):    # duplicate event_id
        con.execute("INSERT INTO events (event_id, event_type, occurred_at, payload_json, source_file, source_line)"
                    " VALUES ('evt_1', 'call.initiated', '2026-10-01T00:00:00+00:00', '{}', 'f', 2)")