import os

import pytest

from solution.crypto import Vault
from solution.db import connect


def test_wrong_keys_are_refused(tmp_path):
    db = str(tmp_path / "t.db")
    connect(db, Vault(os.urandom(32), os.urandom(32))).close()
    with pytest.raises(SystemExit, match="do not match"):
        connect(db, Vault(os.urandom(32), os.urandom(32)))


def test_same_keys_reopen(tmp_path):
    db = str(tmp_path / "t.db")
    v = Vault(os.urandom(32), os.urandom(32))
    connect(db, v).close()
    con = connect(db, v)          # second open: schema already there, fingerprint matches
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def _record(con, key, sha):
    con.execute("INSERT INTO ingested_files (source_key, file_sha256, kind, ingest_order, ingested_at)"
                " VALUES (?, ?, 'leads', 1, '2026-11-13T00:00:00Z')", (key, sha))


def test_ingest_status_same_key_same_bytes_is_done(tmp_path):
    from solution.db import ingest_status
    con = connect(str(tmp_path / "t.db"), Vault(os.urandom(32), os.urandom(32)))
    assert ingest_status(con, "drop_1/leads/motor_renewal/batch_001.csv", "abc") == "new"
    _record(con, "drop_1/leads/motor_renewal/batch_001.csv", "abc")
    assert ingest_status(con, "drop_1/leads/motor_renewal/batch_001.csv", "abc") == "done"
    assert ingest_status(con, "drop_2/leads/motor_renewal/batch_001.csv", "abc") == "new"   # other logical file


def test_ingest_status_changed_content_is_refused(tmp_path):
    from solution.db import ingest_status
    con = connect(str(tmp_path / "t.db"), Vault(os.urandom(32), os.urandom(32)))
    _record(con, "drop_1/events/events_0001.jsonl", "abc")
    with pytest.raises(SystemExit, match="different content"):
        ingest_status(con, "drop_1/events/events_0001.jsonl", "xyz")