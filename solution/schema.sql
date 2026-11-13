-- SQLite schema for the Interaction Ledger. Plaintext PII never appears in any column.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                -- schema_version, key_fingerprint
);

-- One row per input file we have fully ingested.
-- source_key = '<drop folder>/<path inside the drop>' (logical identity), e.g.
-- 'drop_1/leads/motor_renewal/batch_001.csv'. Same key + same SHA-256 -> skip (re-ingest is a no-op).
-- Same key + different SHA-256 -> refuse loudly (never silently double-count).
-- Per-file counters let data_quality.json be rebuilt from the DB at any time.
CREATE TABLE IF NOT EXISTS ingested_files (
    source_key     TEXT PRIMARY KEY,
    file_sha256    TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('leads', 'events')),
    workflow_id    TEXT,               -- leads only
    batch_id       TEXT,               -- leads only (file stem)
    ingest_order   INTEGER NOT NULL,   -- global order: drops as given, files by name
    ingested_at    TEXT NOT NULL,
    rows_total     INTEGER NOT NULL DEFAULT 0,  -- leads: non-blank rows | events: non-empty lines
    rows_rejected  INTEGER NOT NULL DEFAULT 0,
    rows_merged    INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    malformed      INTEGER NOT NULL DEFAULT 0,
    duplicates     INTEGER NOT NULL DEFAULT 0,
    -- events only (D17/A8b): unparseable occurred_at -> skipped; ingest summary only, not data_quality.json
    unusable_timestamp INTEGER NOT NULL DEFAULT 0
);

-- Current state of each lead (identity = workflow_id + phone). Mutable on purpose:
-- duplicates merge into it. The *ledger* below is what must stay append-only.
CREATE TABLE IF NOT EXISTS leads (
    lead_id       INTEGER PRIMARY KEY,
    workflow_id   TEXT NOT NULL,
    phone_bidx    TEXT NOT NULL,       -- HMAC-SHA256(bidx_key, '+91XXXXXXXXXX')
    pii_enc       BLOB NOT NULL,       -- AES-GCM( json of every field marked "pii": true )
    fields_json   TEXT NOT NULL,       -- non-PII fields after normalisation (nulls kept)
    external_ref  TEXT NOT NULL,       -- the schema field marked "reference": true
    batch_id      TEXT NOT NULL,       -- stem of the file that first created the lead; never changes
    UNIQUE (workflow_id, phone_bidx)
);

CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);

-- The interaction ledger: every distinct event, exactly as received (minus plaintext phone).
-- Linking to leads happens at report time, so an orphan today can link tomorrow
-- without ever updating this table.
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY,  -- arrival order
    event_id     TEXT NOT NULL UNIQUE,
    event_type   TEXT NOT NULL,
    workflow_id  TEXT,
    call_id      TEXT,
    occurred_at  TEXT NOT NULL,        -- normalised to UTC ISO-8601
    phone_bidx   TEXT,                 -- NULL if the event has no phone or it is invalid
    phone_enc    BLOB,                 -- raw phone as received, encrypted (audit trail)
    payload_json TEXT NOT NULL,        -- ALLOWLISTED payload keys only (no PII by construction)
    source_file  TEXT NOT NULL,
    source_line  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_link ON events (workflow_id, phone_bidx);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (call_id);

-- Append-only is enforced by the database, not just by convention.
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;