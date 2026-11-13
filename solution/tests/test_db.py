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
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"


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


def test_old_schema_version_is_refused(tmp_path):
    # Decision B2-3: a DB created by a different schema version must be rebuilt, not reused.
    db = str(tmp_path / "t.db")
    v = Vault(os.urandom(32), os.urandom(32))
    con = connect(db, v)
    con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    con.close()
    with pytest.raises(SystemExit, match="delete au_ledger.db and re-ingest"):
        connect(db, v)


_EVT = ("INSERT {verb} INTO events (event_id, event_type, occurred_at, payload_json, source_file, source_line)"
        " VALUES ('evt_1', 'call.initiated', '2026-10-01T00:00:00+00:00', '{{}}', 'f', ?)")


def test_insert_or_replace_cannot_bypass_append_only_ledger(tmp_path):
    # Append-only ledger: OR REPLACE deletes the old row; recursive_triggers makes that hit the trigger.
    import sqlite3
    con = connect(str(tmp_path / "t.db"), Vault(os.urandom(32), os.urandom(32)))
    con.execute(_EVT.format(verb=""), (1,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute(_EVT.format(verb="OR REPLACE"), (2,))
    assert [r[0] for r in con.execute("SELECT source_line FROM events")] == [1]   # original row intact


def test_insert_or_ignore_duplicate_event_is_silently_skipped(tmp_path):
    # Duplicate events (same event_id) must be countable: OR IGNORE -> rowcount 0, no error.
    con = connect(str(tmp_path / "t.db"), Vault(os.urandom(32), os.urandom(32)))
    con.execute(_EVT.format(verb=""), (1,))
    assert con.execute(_EVT.format(verb="OR IGNORE"), (2,)).rowcount == 0
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_rejections_keyed_by_source_key_not_batch(tmp_path):
    # Same batch stem in two drops, same row_number -> both rejections kept.
    con = connect(str(tmp_path / "t.db"), Vault(os.urandom(32), os.urandom(32)))
    for key in ("drop_1/leads/motor_renewal/batch_001.csv", "drop_2/leads/motor_renewal/batch_001.csv"):
        con.execute("INSERT INTO rejections (source_key, workflow_id, batch_id, row_number, reason)"
                    " VALUES (?, 'motor_renewal', 'batch_001', 3, 'invalid phone')", (key,))
    assert con.execute("SELECT COUNT(*) FROM rejections").fetchone()[0] == 2
