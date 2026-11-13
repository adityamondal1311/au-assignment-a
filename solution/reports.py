"""Read side: the five report files (spec README -> "Output contract").

Everything is rebuilt from scratch from the DB on every run (D2, D22): leads + the full event
ledger go through facts.compute_facts; ingest counters come from ingested_files.
Every ordering uses an explicit Python sort key, never DB/insertion order (D12, D13), so
incremental ingestion and all-at-once ingestion produce byte-identical files.

PII: pii_enc is decrypted ONLY to get the canonical phone, which is used for phone_masked and the
D12 tie-break. Names are never read and nothing is printed."""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from solution import config, facts
from solution.crypto import Vault
from solution.normalize import mask_phone

BATCH_SUMMARY_HEADER = ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
                        "in_progress_telephony_failed", "in_progress_dialed", "completed"]
DAILY_OVERVIEW_HEADER = ["workflow_id", "date", "total_calls", "connected_calls",
                         "connectivity_rate", "success_leads"]
LEAD_REPORT_HEADER = ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
                      "total_calls", "connected_calls", "last_call_at", "current_disposition",
                      "ever_success"]
REJECTIONS_HEADER = ["workflow_id", "batch_id", "row_number", "reason"]
# Contract order of data_quality.json keys.
DATA_QUALITY_KEYS = ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                     "duplicate_lead_rows_merged", "field_warnings", "events_total",
                     "malformed_events", "duplicate_events_dropped", "orphan_events",
                     "unknown_disposition_events"]

BUCKETS = [facts.YET_TO_REACH_OUT, facts.IN_PROGRESS_TELEPHONY_FAILED, facts.IN_PROGRESS_DIALED,
           facts.COMPLETED]                     # same order as the batch_summary columns


def _bool(v: bool) -> str:
    return "true" if v else "false"


def format_rate(connected: int, total: int) -> str:
    """Contract: exactly 4 decimals. D6: Python f"{x:.4f}" (0.65625 -> '0.6562')."""
    return f"{connected / total:.4f}"


def format_ist(ts: datetime | None) -> str:
    """Contract: ISO-8601 in IST, e.g. 2026-10-14T17:55:24+05:30; empty if no calls."""
    return "" if ts is None else ts.astimezone(facts.IST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- loading (thin DB layer)
def load_leads(con: sqlite3.Connection, vault: Vault,
               schemas: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Leads as facts expects them, plus the canonical phone (kept in memory only)."""
    leads = []
    for r in con.execute("SELECT lead_id, workflow_id, phone_bidx, batch_id, external_ref, pii_enc"
                         " FROM leads"):
        pii = json.loads(vault.decrypt(r["pii_enc"]))
        leads.append({"lead_id": r["lead_id"], "workflow_id": r["workflow_id"],
                      "phone_bidx": r["phone_bidx"], "batch_id": r["batch_id"],
                      "external_ref": r["external_ref"],
                      "phone": pii[config.phone_field(schemas[r["workflow_id"]])]})
    return leads


def load_events(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"seq": r["seq"], "event_id": r["event_id"], "event_type": r["event_type"],
             "workflow_id": r["workflow_id"], "call_id": r["call_id"],
             "occurred_at": datetime.fromisoformat(r["occurred_at"]),
             "phone_bidx": r["phone_bidx"], "payload": json.loads(r["payload_json"])}
            for r in con.execute("SELECT seq, event_id, event_type, workflow_id, call_id,"
                                 " occurred_at, phone_bidx, payload_json FROM events")]


def _ingest_counters(con: sqlite3.Connection) -> dict[str, int]:
    """Per-file ingest counters summed by kind (D22)."""
    row = con.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN kind = 'leads'  THEN rows_total END), 0)    AS lead_rows_total,"
        " COALESCE(SUM(CASE WHEN kind = 'leads'  THEN rows_rejected END), 0) AS lead_rows_rejected,"
        " COALESCE(SUM(CASE WHEN kind = 'leads'  THEN rows_merged END), 0)   AS duplicate_lead_rows_merged,"
        " COALESCE(SUM(CASE WHEN kind = 'leads'  THEN field_warnings END), 0) AS field_warnings,"
        " COALESCE(SUM(CASE WHEN kind = 'events' THEN rows_total END), 0)    AS events_total,"
        " COALESCE(SUM(CASE WHEN kind = 'events' THEN malformed END), 0)     AS malformed_events,"
        " COALESCE(SUM(CASE WHEN kind = 'events' THEN duplicates END), 0)    AS duplicate_events_dropped"
        " FROM ingested_files").fetchone()
    return dict(row)


# ---------------------------------------------------------------- pure row builders
def batch_summary_rows(leads: list[dict[str, Any]], per_lead: dict[Any, facts.LeadFacts]) -> list[list]:
    """One row per (workflow_id, batch_id) that owns >= 1 lead; sorted workflow_id, batch_id."""
    counts: dict[tuple[str, str], dict[str, int]] = {}
    for lead in leads:
        c = counts.setdefault((lead["workflow_id"], lead["batch_id"]), dict.fromkeys(BUCKETS, 0))
        c[per_lead[lead["lead_id"]].bucket] += 1
    return [[wf, batch, sum(c.values()), *(c[b] for b in BUCKETS)]
            for (wf, batch), c in sorted(counts.items())]


def daily_overview_rows(per_day: dict[tuple[str, Any], facts.DayFacts]) -> list[list]:
    """One row per workflow per IST date with >= 1 call; sorted workflow_id, date."""
    return [[wf, day.isoformat(), d.total_calls, d.connected_calls,
             format_rate(d.connected_calls, d.total_calls), d.success_leads]
            for (wf, day), d in sorted(per_day.items())]


def lead_report_rows(leads: list[dict[str, Any]], per_lead: dict[Any, facts.LeadFacts]) -> list[list]:
    """One row per lead; sorted workflow_id, external_ref, then canonical phone (D12)."""
    rows = []
    for lead in sorted(leads, key=lambda l: (l["workflow_id"], l["external_ref"], l["phone"])):
        f = per_lead[lead["lead_id"]]
        rows.append([lead["workflow_id"], lead["external_ref"], lead["batch_id"],
                     mask_phone(lead["phone"]), f.bucket, f.total_calls, f.connected_calls,
                     format_ist(f.last_call_at), f.current_disposition or "",
                     _bool(f.ever_success)])
    return rows


def rejection_rows(con: sqlite3.Connection) -> list[list]:
    """Sorted workflow_id, batch_id, row_number (numeric); reason breaks a cross-drop tie (D33).
    The rejections table holds no PII by construction."""
    rows = [[r["workflow_id"], r["batch_id"], int(r["row_number"]), r["reason"]]
            for r in con.execute("SELECT workflow_id, batch_id, row_number, reason FROM rejections")]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2], r[3]))


def data_quality(con: sqlite3.Connection, f: facts.Facts) -> dict[str, Any]:
    counters = _ingest_counters(con)
    by_reason = {r["reason"]: r["n"] for r in
                 con.execute("SELECT reason, COUNT(*) AS n FROM rejections GROUP BY reason")}
    values = {**counters,
              "rejections_by_reason": dict(sorted(by_reason.items())),   # observed only, sorted (D10)
              "orphan_events": f.orphan_events,
              "unknown_disposition_events": f.unknown_disposition_events}
    return {k: values[k] for k in DATA_QUALITY_KEYS}


# ---------------------------------------------------------------- writing
def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)                      # always, even with zero data rows
        w.writerows(rows)


def write_reports(con: sqlite3.Connection, vault: Vault, out_dir: Path) -> None:
    schemas = config.load_schemas()
    catalogue = config.load_dispositions()
    leads = load_leads(con, vault, schemas)
    f = facts.compute_facts(leads, load_events(con), catalogue)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "batch_summary.csv", BATCH_SUMMARY_HEADER, batch_summary_rows(leads, f.per_lead))
    _write_csv(out_dir / "daily_overview.csv", DAILY_OVERVIEW_HEADER, daily_overview_rows(f.per_day))
    _write_csv(out_dir / "lead_report.csv", LEAD_REPORT_HEADER, lead_report_rows(leads, f.per_lead))
    _write_csv(out_dir / "rejections.csv", REJECTIONS_HEADER, rejection_rows(con))
    with (out_dir / "data_quality.json").open("w", newline="", encoding="utf-8") as fh:
        json.dump(data_quality(con, f), fh, indent=2)
        fh.write("\n")
