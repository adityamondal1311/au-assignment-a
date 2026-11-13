"""CLI — spec README "What to build" (idempotent + incremental) and D18/D36. Synthetic data only."""
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from solution import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
                "data_quality.json"]
NAMES = ["Asha Rao", "Ravi Kumar", "Meera Iyer", "Old Name", "Priya Das"]
PHONES = ["9876543210", "9123456789", "7000000003", "9000000001"]


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    # Keys via env so the tests never create or read the repo .env.
    monkeypatch.setenv("AU_ENC_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("AU_BIDX_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("AU_DB", raising=False)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def ev(event_id, event_type, at, call_id, phone=None, wf="motor_renewal", **payload):
    e = {"event_id": event_id, "event_type": event_type, "workflow_id": wf, "call_id": call_id,
         "occurred_at": at, "payload": payload}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


def make_drops(root: Path) -> Path:
    """Tiny two-drop fixture exercising merge-across-drops, a late lead resolving an orphan,
    an override arriving in the next drop, a cross-drop duplicate and a malformed line."""
    hdr = "customer_name,phone,policy_number,premium_amount\n"
    _write(root / "drop_1/leads/motor_renewal/batch_001.csv", hdr
           + "Asha Rao,98765 43210,POL-1,100\n"
           + "Ravi Kumar,+91 91234 56789,POL-2,\n"
           + "Bad Phone,12345,POL-X,\n")
    _write(root / "drop_1/leads/motor_renewal/batch_002.csv", hdr
           + "Old Name,9876543210,POL-1,\"₹2,000\"\n")              # merged duplicate
    _write(root / "drop_1/leads/policy_feedback/batch_001.csv",
           "name,mobile,order id\nPriya Das,9000000001,ORD-1\n")
    _write(root / "drop_1/events/events_0001.jsonl", "\n".join([
        ev("e1", "call.initiated", "2026-10-05T10:00:00Z", "c1", "9876543210"),
        ev("e2", "call.connected", "2026-10-05T10:00:05Z", "c1", "9876543210"),
        ev("e3", "disposition.set", "2026-10-05T10:03:00Z", "c1", "9876543210",
           disposition_code="payment_link_sent"),
        ev("e4", "call.initiated", "2026-10-05 18:00:00", "c3", "7000000003"),  # orphan until drop_2
        ev("e5", "call.telephony_failed", "2026-10-05 18:00:02", "c3", "7000000003", reason="sip_503"),
        ev("e6", "call.initiated", "2026-10-06T04:00:00+05:30", "c9", "9000000001",
           wf="policy_feedback"),
        "{broken",
    ]) + "\n")
    _write(root / "drop_2/leads/motor_renewal/batch_003.csv", hdr
           + "Meera Iyer,7000000003,POL-3,\n"
           + ",9123456789,POL-2,\n")                                 # rejected (missing name)
    _write(root / "drop_2/events/events_1001.jsonl", "\n".join([
        ev("e7", "disposition.override", "2026-10-06T09:00:00Z", "c1", disposition_code="renewed",
           overridden_by="sup_1", reason="customer paid"),
        ev("e3", "disposition.set", "2026-10-05T10:03:00Z", "c1", "9876543210",
           disposition_code="payment_link_sent"),                    # cross-drop duplicate
        ev("e8", "call.initiated", "2026-10-06T10:00:00Z", "c2", "9123456789"),
    ]) + "\n")
    return root


def run(*argv) -> int:
    return cli.main([str(a) for a in argv])


def report_bytes(db, out) -> dict[str, bytes]:
    assert run("report", "--out", out, "--db", db) == 0
    return {name: (Path(out) / name).read_bytes() for name in REPORT_FILES}


def snapshot(db) -> dict:
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("meta", "ingested_files", "leads", "rejections", "events")}
    finally:
        con.close()


# ---------------------------------------------------------------------------------------------
def test_incremental_equals_all_at_once_byte_identical(tmp_path):
    # Spec: "Ingesting drop_1 then drop_2 must give the same reports as ingesting everything at once."
    data = make_drops(tmp_path / "data")
    a, b, c = (tmp_path / f"{n}.db" for n in "abc")
    assert run("ingest", data / "drop_1", "--db", a) == 0
    assert run("ingest", data / "drop_2", "--db", a) == 0
    assert run("ingest", data / "drop_1", data / "drop_2", "--db", b) == 0
    assert run("ingest", data, "--db", c) == 0                       # D18: root expands, sorted
    ra, rb, rc = (report_bytes(db, tmp_path / f"out_{db.stem}") for db in (a, b, c))
    assert ra == rb == rc

    # The fixture is not trivial: late lead resolved the orphan, override won, merge happened.
    dq = json.loads(ra["data_quality.json"])
    assert dq["orphan_events"] == 0 and dq["duplicate_events_dropped"] == 1
    assert dq["malformed_events"] == 1 and dq["duplicate_lead_rows_merged"] == 1
    lead_report = ra["lead_report.csv"].decode("utf-8")
    assert "motor_renewal,POL-1,batch_001,+91******3210,completed,1,1,2026-10-05T15:30:00+05:30,renewed,true" \
        in lead_report
    assert "motor_renewal,POL-3,batch_003,+91******0003,in_progress_telephony_failed,1,0," in lead_report


def test_reingest_changes_nothing(tmp_path, capsys):
    # Spec: "Re-running ingest on a drop you already ingested must change nothing." (D3)
    data = make_drops(tmp_path / "data")
    db = tmp_path / "x.db"
    assert run("ingest", data, "--db", db) == 0
    before, rep_before = snapshot(db), report_bytes(db, tmp_path / "r1")
    capsys.readouterr()
    assert run("ingest", data / "drop_1", data / "drop_2", "--db", db) == 0
    out = capsys.readouterr().out
    assert out.count("already ingested, skipped") == 6
    assert snapshot(db) == before
    assert report_bytes(db, tmp_path / "r2") == rep_before


def test_changed_file_refused_before_any_row_is_written(tmp_path, capsys):
    # D36: every file of every given drop is checked BEFORE ingesting any. drop_2 comes first on
    # the command line and is new, but drop_1's events file changed -> nothing at all is written.
    data = make_drops(tmp_path / "data")
    db = tmp_path / "x.db"
    assert run("ingest", data / "drop_1", "--db", db) == 0
    before = snapshot(db)
    with (data / "drop_1/events/events_0001.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(ev("e99", "call.initiated", "2026-10-07T10:00:00Z", "c99", "9876543210") + "\n")
    capsys.readouterr()
    assert run("ingest", data / "drop_2", data / "drop_1", "--db", db) == 1
    err = capsys.readouterr().err
    assert "drop_1/events/events_0001.jsonl" in err and "different content" in err
    assert snapshot(db) == before


def test_same_identity_twice_with_different_bytes_in_one_command_refused(tmp_path):
    # D36 + D7: two folders both named drop_1 with different content in one command -> refuse, write nothing.
    make_drops(tmp_path / "one")
    make_drops(tmp_path / "two")
    _write(tmp_path / "two/drop_1/leads/motor_renewal/batch_001.csv",
           "customer_name,phone,policy_number\nX,9876543210,POL-1\n")
    db = tmp_path / "x.db"
    assert run("ingest", tmp_path / "one/drop_1", tmp_path / "two/drop_1", "--db", db) == 1
    assert snapshot(db)["ingested_files"] == [] and snapshot(db)["leads"] == []


def test_unknown_workflow_folder_refused_before_any_row(tmp_path):
    # Schema-driven: a workflow folder without a schema is refused up front (D36 pre-check).
    data = make_drops(tmp_path / "data")
    _write(data / "drop_1/leads/zzz_unknown/batch_001.csv", "name,phone\nA,9876543210\n")
    db = tmp_path / "x.db"
    assert run("ingest", data / "drop_1", "--db", db) == 1
    assert snapshot(db)["ingested_files"] == []


def test_summary_prints_counts_only_never_pii(tmp_path, capsys):
    # CLAUDE.md: never print names, phones or raw rows; summary includes unusable_timestamp (D17).
    data = make_drops(tmp_path / "data")
    assert run("ingest", data, "--db", tmp_path / "x.db") == 0
    assert run("report", "--out", tmp_path / "r", "--db", tmp_path / "x.db") == 0
    out = capsys.readouterr()
    text = out.out + out.err
    assert "drop_1/leads/motor_renewal/batch_001.csv: rows=3 rejected=1 merged=0 field_warnings=0" in text
    assert "drop_1/events/events_0001.jsonl: lines=7 stored=6 malformed=1 duplicates=0" \
           " unusable_timestamp=0" in text
    assert "drop_2/events/events_1001.jsonl: lines=3 stored=2 malformed=0 duplicates=1" in text
    for secret in NAMES + PHONES + ["98765 43210", "+91 91234"]:
        assert secret not in text


def test_exit_codes_usage_errors(tmp_path, capsys):
    data = make_drops(tmp_path / "data")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run("ingest", tmp_path / "nope", "--db", tmp_path / "x.db") == 2   # missing path
    assert run("ingest", empty, "--db", tmp_path / "x.db") == 2              # not a drop, no drop_*
    assert run("report", "--out", tmp_path / "r", "--db", tmp_path / "missing.db") == 2
    assert not (tmp_path / "missing.db").exists()                            # report never creates a DB
    assert run() == 2                                                        # no command
    assert run("ingest") == 2                                                # no PATH
    assert run("report", "--db", tmp_path / "x.db") == 2                     # --out required
    assert run("ingest", data, "--db", tmp_path / "x.db") == 0
    capsys.readouterr()


def test_missing_path_writes_nothing(tmp_path):
    # A bad PATH anywhere in the list is detected before connecting: no DB is created.
    data = make_drops(tmp_path / "data")
    db = tmp_path / "x.db"
    assert run("ingest", data / "drop_1", tmp_path / "nope", "--db", db) == 2
    assert not db.exists()


def test_default_db_from_au_db_env(tmp_path, monkeypatch):
    # CLAUDE.md: --db default is env AU_DB, else au_ledger.db.
    data = make_drops(tmp_path / "data")
    db = tmp_path / "from_env.db"
    monkeypatch.setenv("AU_DB", str(db))
    assert run("ingest", data / "drop_1") == 0
    assert db.exists()
    assert run("report", "--out", tmp_path / "r") == 0
    assert (tmp_path / "r" / "lead_report.csv").exists()


def test_python_dash_m_solution_end_to_end(tmp_path):
    # `python -m solution ingest|report` works as a real process and exits 0.
    data = make_drops(tmp_path / "data")
    db, out = tmp_path / "x.db", tmp_path / "reports"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for argv in (["ingest", str(data / "drop_1"), str(data / "drop_2"), "--db", str(db)],
                 ["report", "--out", str(out), "--db", str(db)]):
        proc = subprocess.run([sys.executable, "-m", "solution", *argv], cwd=REPO_ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr
    assert sorted(p.name for p in out.iterdir()) == sorted(REPORT_FILES)
    proc = subprocess.run([sys.executable, "-m", "solution", "ingest", str(tmp_path / "nope"),
                           "--db", str(db)], cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert proc.returncode == 2
