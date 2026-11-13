"""Thin database layer: open, create schema, guard against wrong keys."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from solution.crypto import Vault

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "2"


def default_db_path() -> str:
    return os.environ.get("AU_DB", "au_ledger.db")


def connect(db_path: str, vault: Vault) -> sqlite3.Connection:
    # isolation_level=None -> we control transactions explicitly with BEGIN/COMMIT.
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    # INSERT OR REPLACE resolves a conflict by deleting the old row; SQLite only fires DELETE
    # triggers for that when recursive_triggers is ON. Without it, OR REPLACE would bypass the
    # append-only ledger. (A BEFORE INSERT trigger would also abort INSERT OR IGNORE, which we
    # rely on to count duplicate events, so this pragma is the right fix.)
    con.execute("PRAGMA recursive_triggers = ON")
    con.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    _check_schema_version(con)
    _check_keys(con, vault)
    return con


def _check_schema_version(con: sqlite3.Connection) -> None:
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None and con.execute("SELECT 1 FROM meta LIMIT 1").fetchone() is None:
        return                                            # brand-new database; _check_keys records it
    stored = None if row is None else row["value"]
    if stored != SCHEMA_VERSION:
        raise SystemExit(
            f"Database schema version is {stored!r}, this code expects {SCHEMA_VERSION!r}.\n"
            "delete au_ledger.db and re-ingest.")


def _check_keys(con: sqlite3.Connection, vault: Vault) -> None:
    fp = vault.key_fingerprint()
    row = con.execute("SELECT value FROM meta WHERE key = 'key_fingerprint'").fetchone()
    if row is None:
        con.execute("INSERT INTO meta (key, value) VALUES ('key_fingerprint', ?), ('schema_version', ?)",
                    (fp, SCHEMA_VERSION))
    elif row["value"] != fp:
        raise SystemExit(
            "Encryption keys do not match the keys this database was created with.\n"
            "Restore the original .env / AU_ENC_KEY / AU_BIDX_KEY, or start a new database.")


def ingest_status(con: sqlite3.Connection, source_key: str, sha256: str) -> str:
    """'new' -> ingest it; 'done' -> same file, same bytes, skip (idempotent).
    Same source_key with DIFFERENT bytes -> refuse: re-ingesting would double-count."""
    row = con.execute("SELECT file_sha256 FROM ingested_files WHERE source_key = ?", (source_key,)).fetchone()
    if row is None:
        return "new"
    if row["file_sha256"] == sha256:
        return "done"
    raise SystemExit(f"{source_key} was already ingested with different content. "
                     "Refusing to double-count; start a new database to load a corrected drop.")


def next_ingest_order(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COALESCE(MAX(ingest_order), 0) + 1 FROM ingested_files").fetchone()[0]