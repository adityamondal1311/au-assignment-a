"""Reports — spec README "Output contract". Synthetic data only."""
import json
import os

import pytest

from solution import reports
from solution.config import load_schemas
from solution.crypto import Vault
from solution.db import connect
from solution.ingest_events import ingest_event_file
from solution.ingest_leads import ingest_lead_file

SCHEMAS = load_schemas()
HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"
FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
         "data_quality.json"]


@pytest.fixture
def vault():
    return Vault(os.urandom(32), os.urandom(32))


@pytest.fixture
def con(tmp_path, vault):
    c = connect(str(tmp_path / "t.db"), vault)
    yield c
    c.close()


def add_leads(con, vault, tmp_path, text, wf="motor_renewal", name="batch_001.csv", drop="drop_1"):
    drop_dir = tmp_path / drop
    p = drop_dir / "leads" / wf / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return ingest_lead_file(con, vault, SCHEMAS, drop_dir, p)


def ev(event_id, event_type, at, call_id="call_1", phone="9876543210", wf="motor_renewal", **payload):
    e = {"event_id": event_id, "event_type": event_type, "workflow_id": wf, "call_id": call_id,
         "occurred_at": at, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


def add_events(con, vault, tmp_path, lines, name="events_0001.jsonl", drop="drop_1"):
    drop_dir = tmp_path / drop
    p = drop_dir / "events" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return ingest_event_file(con, vault, drop_dir, p)


def run(con, vault, tmp_path):
    out = tmp_path / "out" / "nested"            # out_dir created if missing
    reports.write_reports(con, vault, out)
    return out


def read(out, name):
    return (out / name).read_text(encoding="utf-8")


def rows(out, name):
    return [line.split(",") for line in read(out, name).splitlines()[1:]]


# ---------------------------------------------------------------------------------------------
def test_exact_headers_and_zero_row_files_still_have_headers(tmp_path, con, vault):
    # Contract: CSVs have a header row (exact column names, no index column); empty DB -> header only.
    out = run(con, vault, tmp_path)
    assert sorted(p.name for p in out.iterdir()) == sorted(FILES)
    assert read(out, "batch_summary.csv") == (
        "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,"
        "in_progress_dialed,completed\n")
    assert read(out, "daily_overview.csv") == (
        "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads\n")
    assert read(out, "lead_report.csv") == (
        "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,"
        "last_call_at,current_disposition,ever_success\n")
    assert read(out, "rejections.csv") == "workflow_id,batch_id,row_number,reason\n"


def test_data_quality_exact_key_order_zeros_and_trailing_newline(tmp_path, con, vault):
    # Contract: data_quality.json keys in the documented order.
    out = run(con, vault, tmp_path)
    text = read(out, "data_quality.json")
    assert text.endswith("}\n")
    dq = json.loads(text)
    assert list(dq) == ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason",
                        "duplicate_lead_rows_merged", "field_warnings", "events_total",
                        "malformed_events", "duplicate_events_dropped", "orphan_events",
                        "unknown_disposition_events"]
    assert dq["rejections_by_reason"] == {}
    assert all(v == 0 for k, v in dq.items() if k != "rejections_by_reason")


def test_data_quality_counters_and_rejections_by_reason_observed_only(tmp_path, con, vault):
    # D10: only observed reasons, keys sorted. D22: ingest counters summed over files,
    # orphan/unknown from report-time facts.
    add_leads(con, vault, tmp_path, HDR
              + "Asha,9876543210,POL-1,,,\n"
              + "Ravi,12345,POL-2,,,\n"                 # invalid_phone
              + ",9123456789,POL-3,,,\n"                # missing_required:customer_name
              + "Asha,9876543210,POL-1,31/02/2026,,\n") # merged duplicate + 1 field warning
    add_events(con, vault, tmp_path, [
        ev("e1", "call.initiated", "2026-10-05T10:00:00Z"),
        ev("e1", "call.initiated", "2026-10-05T10:00:00Z"),                 # duplicate
        "{not json",                                                        # malformed
        ev("e2", "call.initiated", "2026-10-05T10:00:00Z", phone="7000000000"),  # orphan
        ev("e3", "disposition.set", "2026-10-05T10:01:00Z", disposition_code="nope"),  # unknown
    ])
    dq = json.loads(read(run(con, vault, tmp_path), "data_quality.json"))
    assert dq == {"lead_rows_total": 4, "lead_rows_rejected": 2,
                  "rejections_by_reason": {"invalid_phone": 1, "missing_required:customer_name": 1},
                  "duplicate_lead_rows_merged": 1, "field_warnings": 1, "events_total": 5,
                  "malformed_events": 1, "duplicate_events_dropped": 1, "orphan_events": 1,
                  "unknown_disposition_events": 1}
    assert list(dq["rejections_by_reason"]) == sorted(dq["rejections_by_reason"])


def test_rejections_sorted_numerically_and_contain_no_pii(tmp_path, con, vault):
    # Contract: rejections.csv sorted by workflow_id, batch_id, row_number (numeric: 10 after 9).
    # "No PII": no names, no phones.
    body = "".join(f"Name{i},98765432{i:02d},,,,\n" for i in range(1, 12))  # missing policy_number
    add_leads(con, vault, tmp_path, HDR + body, name="batch_002.csv")
    add_leads(con, vault, tmp_path, HDR + "Zed,123,POL-9,,,\n", name="batch_001.csv")
    out = run(con, vault, tmp_path)
    got = rows(out, "rejections.csv")
    assert got[0] == ["motor_renewal", "batch_001", "1", "invalid_phone"]
    assert [int(r[2]) for r in got[1:]] == list(range(1, 12))
    assert all(r[:2] == ["motor_renewal", "batch_002"] and r[3] == "missing_required:policy_number"
               for r in got[1:])
    text = read(out, "rejections.csv")
    assert "Name" not in text and "Zed" not in text and "98765432" not in text and "123," not in text


def test_lead_report_formats_and_bucket(tmp_path, con, vault):
    # Contract: phone_masked '+91******' + last 4; last_call_at ISO-8601 IST with seconds only;
    # booleans true/false; empty last_call_at / current_disposition when there is none.
    add_leads(con, vault, tmp_path, HDR + "Asha,98765 43210,POL-1,,,\nRavi,9123456789,POL-2,,,\n")
    add_events(con, vault, tmp_path, [
        ev("e1", "call.initiated", "2026-10-05T13:20:21.654321Z"),
        ev("e2", "call.connected", "2026-10-05T13:20:30Z"),
        ev("e3", "disposition.set", "2026-10-05T13:25:00Z", disposition_code="renewed"),
    ])
    out = run(con, vault, tmp_path)
    assert rows(out, "lead_report.csv") == [
        ["motor_renewal", "POL-1", "batch_001", "+91******3210", "completed", "1", "1",
         "2026-10-05T18:50:21+05:30", "renewed", "true"],
        ["motor_renewal", "POL-2", "batch_001", "+91******6789", "yet_to_reach_out", "0", "0",
         "", "", "false"],
    ]
    text = read(out, "lead_report.csv")
    assert "Asha" not in text and "9876543210" not in text


def test_lead_report_sorted_by_workflow_ref_then_phone(tmp_path, con, vault):
    # Contract sort workflow_id, external_ref; D12 tie-break by canonical phone (not ingest order).
    add_leads(con, vault, tmp_path, HDR
              + "A,9999999999,POL-1,,,\nB,8888888888,POL-1,,,\nC,7777777777,POL-0,,,\n")
    add_leads(con, vault, tmp_path,
              "customer_name,phone,order_id\nD,6666666666,ORD-1\n", wf="policy_feedback")
    got = [(r[0], r[1], r[3]) for r in rows(run(con, vault, tmp_path), "lead_report.csv")]
    assert got == [("motor_renewal", "POL-0", "+91******7777"),
                   ("motor_renewal", "POL-1", "+91******8888"),
                   ("motor_renewal", "POL-1", "+91******9999"),
                   ("policy_feedback", "ORD-1", "+91******6666")]


def test_rate_formatting_exactly_four_decimals():
    # Contract: connectivity_rate with exactly 4 decimals. D6: f"{x:.4f}" (0.65625 -> 0.6562).
    assert reports.format_rate(84, 128) == "0.6562"
    assert reports.format_rate(9, 16) == "0.5625"
    assert reports.format_rate(1, 1) == "1.0000"
    assert reports.format_rate(0, 3) == "0.0000"
    assert reports.format_rate(1, 3) == "0.3333"


def test_daily_overview_ist_date_rate_and_sort(tmp_path, con, vault):
    # Contract: one row per workflow per IST date with >= 1 call; sorted workflow_id, date.
    # Spec: a call's date is the IST date of call.initiated.
    add_leads(con, vault, tmp_path, HDR + "A,9876543210,POL-1,,,\nB,9123456789,POL-2,,,\n")
    add_events(con, vault, tmp_path, [
        ev("e1", "call.initiated", "2026-10-05T20:00:00Z", call_id="c1"),     # IST 2026-10-06
        ev("e2", "call.connected", "2026-10-05T20:00:10Z", call_id="c1"),
        ev("e3", "disposition.set", "2026-10-05T20:05:00Z", call_id="c1", disposition_code="renewed"),
        ev("e4", "call.initiated", "2026-10-06 09:00:00", call_id="c2", phone="9123456789"),  # naive=IST
        ev("e5", "call.initiated", "2026-10-04T10:00:00+05:30", call_id="c3"),
    ])
    assert rows(run(con, vault, tmp_path), "daily_overview.csv") == [
        ["motor_renewal", "2026-10-04", "1", "0", "0.0000", "0"],
        ["motor_renewal", "2026-10-06", "2", "1", "0.5000", "1"],
    ]


def test_batch_summary_counts_buckets_and_skips_batches_without_leads(tmp_path, con, vault):
    # Contract: batch_summary sorted by workflow_id, batch_id; bucket columns sum to total_leads.
    add_leads(con, vault, tmp_path, HDR + "A,9876543210,POL-1,,,\nB,9123456789,POL-2,,,\n",
              name="batch_002.csv")
    add_leads(con, vault, tmp_path, HDR + "C,7000000001,POL-3,,,\n", name="batch_001.csv")
    add_leads(con, vault, tmp_path, HDR + "D,123,POL-4,,,\n", name="batch_003.csv")  # all rejected
    add_events(con, vault, tmp_path, [
        ev("e1", "call.initiated", "2026-10-05T10:00:00Z", call_id="c1"),
        ev("e2", "call.telephony_failed", "2026-10-05T10:00:01Z", call_id="c1", reason="sip_503"),
        ev("e3", "call.initiated", "2026-10-05T10:00:00Z", call_id="c2", phone="9123456789"),
        ev("e4", "call.initiated", "2026-10-05T10:00:00Z", call_id="c3", phone="7000000001"),
        ev("e5", "disposition.set", "2026-10-05T10:02:00Z", call_id="c3", phone="7000000001",
           disposition_code="not_interested"),
    ])
    assert rows(run(con, vault, tmp_path), "batch_summary.csv") == [
        ["motor_renewal", "batch_001", "1", "0", "0", "0", "1"],
        ["motor_renewal", "batch_002", "2", "0", "1", "1", "0"],
    ]


def test_crlf_free_output(tmp_path, con, vault):
    # Contract allows \n or \r\n; we always write \n (csv lineterminator) so output is stable on Windows.
    add_leads(con, vault, tmp_path, HDR + "A,9876543210,POL-1,,,\n")
    out = run(con, vault, tmp_path)
    for name in FILES:
        assert b"\r" not in (out / name).read_bytes()
