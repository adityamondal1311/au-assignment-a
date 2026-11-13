"""Write side for JSONL event files (spec README -> "Rules (the spec)" -> "Event files").

Only steps 1 (malformed) and 2 (duplicate) are decided here. Orphan and unknown-disposition are
derived at report time from the full ledger, so late leads resolve them (D2, D22).
One file = one transaction: events and the ingested_files record commit together (D3).
Never prints or logs phones or raw lines."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution import db
from solution.crypto import Vault
from solution.normalize import normalize_phone, parse_timestamp

ALLOWED_PAYLOAD_KEYS = ("disposition_code", "overridden_by", "reason", "duration_sec", "end_reason")
_BOM = b"\xef\xbb\xbf"


def _str_or_none(v: Any) -> str | None:
    """D40: only JSON strings count; anything else is treated as missing."""
    return v if isinstance(v, str) else None


def _phone_value(v: Any) -> str | None:
    """D40: a JSON integer phone (not bool) is accepted as its decimal text."""
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return _str_or_none(v)


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    """Spec step 1 "Malformed": not valid JSON, or no event_id -> None.
    D41: undecodable UTF-8 is not valid JSON. D15: non-object, or event_id missing/empty/non-string."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    event_id = obj.get("event_id")
    if not isinstance(event_id, str) or event_id == "":
        return None
    return obj


def _payload_json(payload: Any) -> str:
    """D28: allowlist only; raw payloads are never stored."""
    kept = {k: payload[k] for k in ALLOWED_PAYLOAD_KEYS if k in payload} if isinstance(payload, dict) else {}
    return json.dumps(kept, sort_keys=True)


def ingest_event_file(con: sqlite3.Connection, vault: Vault, drop_dir: Path, path: Path) -> dict[str, Any]:
    """Ingest drop_dir/events/<file>.jsonl. Returns per-file stats (counts only)."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    source_key = f"{drop_dir.name}/{path.relative_to(drop_dir).as_posix()}"   # D7
    if db.ingest_status(con, source_key, sha) == "done":    # changed bytes -> raises SystemExit
        return {"skipped": True, "source_key": source_key}

    stats = {"skipped": False, "source_key": source_key, "lines_total": 0, "malformed": 0,
             "duplicates": 0, "unusable_timestamp": 0, "stored": 0}
    if data.startswith(_BOM):                                # D41
        data = data[len(_BOM):]

    con.execute("BEGIN")
    try:
        # D41: bytes.splitlines() splits only on \n, \r\n, \r; terminators are removed, nothing else.
        for line_no, raw in enumerate(data.splitlines(), start=1):
            if raw == b"":                                   # genuinely empty: skipped, not counted
                continue
            stats["lines_total"] += 1                        # spec: every non-empty line (D25)
            obj = _parse_line(raw)
            if obj is None:
                stats["malformed"] += 1
                continue
            ts = parse_timestamp(obj.get("occurred_at"))
            if ts is None:                                   # D17
                stats["unusable_timestamp"] += 1
                continue
            phone = _phone_value(obj.get("phone"))
            canonical = normalize_phone(phone)
            event_type = obj.get("event_type")
            cur = con.execute(
                "INSERT OR IGNORE INTO events (event_id, event_type, workflow_id, call_id, occurred_at,"
                " phone_bidx, phone_enc, payload_json, source_file, source_line)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obj["event_id"],
                 event_type if isinstance(event_type, str) else "",          # D39: NOT NULL always met
                 _str_or_none(obj.get("workflow_id")),
                 _str_or_none(obj.get("call_id")),
                 ts.astimezone(timezone.utc).isoformat(timespec="microseconds"),   # D42
                 vault.blind_index(canonical) if canonical else None,
                 vault.encrypt(phone) if phone is not None else None,
                 _payload_json(obj.get("payload")),
                 source_key, line_no))
            # Every NOT NULL column is filled above, so IGNORE can only mean UNIQUE(event_id):
            # spec step 2 "Duplicate: an event_id already seen (in any drop)".
            if cur.rowcount == 0:
                stats["duplicates"] += 1
            else:
                stats["stored"] += 1
        con.execute("INSERT INTO ingested_files (source_key, file_sha256, kind, ingest_order, ingested_at,"
                    " rows_total, malformed, duplicates, unusable_timestamp)"
                    " VALUES (?, ?, 'events', ?, ?, ?, ?, ?, ?)",
                    (source_key, sha, db.next_ingest_order(con),
                     datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     stats["lines_total"], stats["malformed"], stats["duplicates"],
                     stats["unusable_timestamp"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return stats
