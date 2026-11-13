"""Write side for lead CSVs (spec README -> "Rules (the spec)" -> "Lead files").

One file = one transaction: lead rows, rejections and the ingested_files record commit together,
so a crash mid-file leaves nothing behind and a re-run starts clean (D3).
Never prints or logs names, phones or raw rows."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from solution import config, db
from solution.crypto import Vault
from solution.normalize import (normalize_header, normalize_phone, parse_date, parse_decimal,
                                parse_enum)


def _column_map(header: list[str], schema: dict[str, Any]) -> tuple[dict[str, int], list[str]]:
    """field name -> column index. A header matches the normalised field name or any normalised
    alias; unmatched columns are ignored. Two columns for one field: leftmost wins (D20)."""
    lookup: dict[str, str] = {}
    for f in schema["fields"]:
        for label in [f["name"], *f.get("aliases", [])]:
            key = normalize_header(label)
            if lookup.get(key, f["name"]) != f["name"]:
                raise ValueError(f"{schema['workflow_id']}: header {key!r} maps to two fields")
            lookup[key] = f["name"]
    colmap: dict[str, int] = {}
    collisions: list[str] = []
    for idx, h in enumerate(header):
        field = lookup.get(normalize_header(h))
        if field is None:
            continue
        if field in colmap:
            collisions.append(field)          # header text only, never a cell value
        else:
            colmap[field] = idx
    return colmap, collisions


def _parse_value(field: dict[str, Any], raw: str) -> Any:
    t = field["type"]
    if t == "string":
        return raw
    if t == "phone":
        return normalize_phone(raw)
    if t == "date":
        return parse_date(raw)
    if t == "decimal":
        return parse_decimal(raw)
    if t == "enum":
        return parse_enum(raw, field["values"])
    raise ValueError(f"unknown field type {t!r} for {field['name']}")


def _parse_row(row: list[str], colmap: dict[str, int],
               schema: dict[str, Any]) -> tuple[dict[str, Any], str | None, int]:
    """Spec "Rejection": walk fields in schema order; the first failure is the single reason.
    Returns (values, reason or None, field_warnings)."""
    values: dict[str, Any] = {}
    reason: str | None = None
    warnings = 0
    for f in schema["fields"]:
        name = f["name"]
        idx = colmap.get(name)
        raw = row[idx].strip() if idx is not None and idx < len(row) else ""
        if raw == "":                                     # "An empty value counts as missing"
            values[name] = None
            if f.get("required") and reason is None:
                reason = f"missing_required:{name}"
            continue
        parsed = _parse_value(f, raw)
        values[name] = parsed
        if parsed is None:                                # present but invalid
            if f["type"] == "phone":
                reason = reason or "invalid_phone"        # D8
            elif f.get("required"):
                reason = reason or f"missing_required:{name}"   # D37
            else:
                warnings += 1                             # optional -> null + one field warning
    return values, reason, warnings


def _to_json(v: Any) -> Any:
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)                                     # already quantized: "27554.00"
    return v


def _phone_field(schema: dict[str, Any]) -> str:
    phones = [f["name"] for f in schema["fields"] if f["type"] == "phone"]
    if len(phones) != 1:
        raise ValueError(f"{schema['workflow_id']}: expected exactly one phone field, got {phones}")
    return phones[0]


def _overlay(target: dict[str, Any], new: dict[str, Any]) -> None:
    """Duplicates: overwrite only where the new value is non-null (null never erases)."""
    for k, v in new.items():
        if v is not None:
            target[k] = v


def effective_fields(fields: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Spec "Defaults ... apply after merging, to fields that are still null" (D38: applied on
    read, never stored)."""
    out = dict(fields)
    for name, default in config.defaults(schema).items():
        if out.get(name) is None:
            out[name] = default
    return out


def ingest_lead_file(con: sqlite3.Connection, vault: Vault, schemas: dict[str, dict[str, Any]],
                     drop_dir: Path, path: Path) -> dict[str, Any]:
    """Ingest drop_dir/leads/<workflow_id>/<batch>.csv. Returns per-file stats."""
    workflow_id = path.parent.name
    schema = schemas.get(workflow_id)
    if schema is None:
        raise SystemExit(f"Unknown workflow {workflow_id!r} for {path.name}: "
                         f"no schema in assignment-a-data/schemas (known: {sorted(schemas)}).")
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    source_key = f"{drop_dir.name}/{path.relative_to(drop_dir).as_posix()}"   # D7
    if db.ingest_status(con, source_key, sha) == "done":    # changed bytes -> raises SystemExit
        return {"skipped": True, "source_key": source_key}

    batch_id = path.stem
    pii_names = set(config.pii_fields(schema))
    ref_name = config.reference_field(schema)
    phone_name = _phone_field(schema)
    stats = {"skipped": False, "source_key": source_key, "workflow_id": workflow_id,
             "batch_id": batch_id, "rows_total": 0, "rows_rejected": 0, "rows_merged": 0,
             "field_warnings": 0, "header_collisions": []}

    con.execute("BEGIN")
    try:
        reader = csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""))   # D26
        header = next(reader, [])
        colmap, stats["header_collisions"] = _column_map(header, schema)
        row_number = 0
        for row in reader:
            if all(cell.strip() == "" for cell in row):   # blank: EVERY cell, mapped or not (D19)
                continue
            row_number += 1
            stats["rows_total"] += 1
            values, reason, warnings = _parse_row(row, colmap, schema)
            if reason is not None:
                con.execute("INSERT INTO rejections (source_key, workflow_id, batch_id, row_number, reason)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (source_key, workflow_id, batch_id, row_number, reason))
                stats["rows_rejected"] += 1
                continue                                  # warnings of rejected rows not counted
            stats["field_warnings"] += warnings
            pii = {k: v for k, v in values.items() if k in pii_names}
            fields = {k: _to_json(v) for k, v in values.items() if k not in pii_names}
            bidx = vault.blind_index(values[phone_name])
            existing = con.execute("SELECT lead_id, pii_enc, fields_json, external_ref FROM leads"
                                   " WHERE workflow_id = ? AND phone_bidx = ?",
                                   (workflow_id, bidx)).fetchone()
            if existing is not None:                      # merged duplicate; batch_id never changes
                stats["rows_merged"] += 1
                old_pii = json.loads(vault.decrypt(existing["pii_enc"]))
                old_fields = json.loads(existing["fields_json"])
                _overlay(old_pii, pii)
                _overlay(old_fields, fields)
                ref = values[ref_name] if values[ref_name] is not None else existing["external_ref"]  # D11
                con.execute("UPDATE leads SET pii_enc = ?, fields_json = ?, external_ref = ?"
                            " WHERE lead_id = ?",
                            (vault.encrypt(json.dumps(old_pii, sort_keys=True)),
                             json.dumps(old_fields, sort_keys=True), ref, existing["lead_id"]))
            else:
                con.execute("INSERT INTO leads (workflow_id, phone_bidx, pii_enc, fields_json,"
                            " external_ref, batch_id) VALUES (?, ?, ?, ?, ?, ?)",
                            (workflow_id, bidx, vault.encrypt(json.dumps(pii, sort_keys=True)),
                             json.dumps(fields, sort_keys=True), values[ref_name], batch_id))
        con.execute("INSERT INTO ingested_files (source_key, file_sha256, kind, workflow_id, batch_id,"
                    " ingest_order, ingested_at, rows_total, rows_rejected, rows_merged, field_warnings)"
                    " VALUES (?, ?, 'leads', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_key, sha, workflow_id, batch_id, db.next_ingest_order(con),
                     datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     stats["rows_total"], stats["rows_rejected"], stats["rows_merged"],
                     stats["field_warnings"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return stats
