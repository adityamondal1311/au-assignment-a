"""Event ingestion — spec README "Rules (the spec)" -> "Event files". Synthetic data only."""
import json
import os

import pytest

from solution import ingest_events
from solution.crypto import Vault
from solution.db import connect
from solution.ingest_events import ingest_event_file


@pytest.fixture
def vault():
    return Vault(os.urandom(32), os.urandom(32))


@pytest.fixture
def con(tmp_path, vault):
    c = connect(str(tmp_path / "t.db"), vault)
    yield c
    c.close()


def ev(event_id="evt_1", **over):
    """One well-formed event as a JSON line (no terminator)."""
    e = {"event_id": event_id, "event_type": "call.initiated", "workflow_id": "motor_renewal",
         "call_id": "call_1", "occurred_at": "2026-10-05T13:20:21Z", "phone": "98765 43210",
         "payload": {}}
    e.update(over)
    return json.dumps(e)


def write_events(tmp_path, content, drop="drop_1", name="events_001.jsonl"):
    """content: list of lines (joined with '\\n' + trailing '\\n') or raw bytes."""
    drop_dir = tmp_path / drop
    p = drop_dir / "events" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, list):
        content = ("\n".join(content) + "\n").encode("utf-8")
    p.write_bytes(content)
    return drop_dir, p


def ingest(con, vault, drop_dir, p):
    return ingest_event_file(con, vault, drop_dir, p)


def events(con):
    return {r["event_id"]: dict(r) for r in con.execute("SELECT * FROM events")}


def snapshot(con):
    return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("events", "ingested_files")}


# 1 ---------------------------------------------------------------------------------------------
def test_truncated_json_is_malformed(tmp_path, con, vault):
    # Spec step 1 "Malformed: the line is not valid JSON ... count it and skip it".
    d, p = write_events(tmp_path, [ev("evt_1"), ev("evt_2")[:-10]])
    stats = ingest(con, vault, d, p)
    assert stats["malformed"] == 1 and stats["lines_total"] == 2
    assert set(events(con)) == {"evt_1"}


# 2 ---------------------------------------------------------------------------------------------
def test_valid_json_that_is_not_an_object_is_malformed(tmp_path, con, vault):
    # D15: a JSON array / string / number is not an event.
    d, p = write_events(tmp_path, ['["evt_1"]', '"evt_2"', "42"])
    stats = ingest(con, vault, d, p)
    assert stats["malformed"] == 3 and events(con) == {}


# 3 ---------------------------------------------------------------------------------------------
def test_missing_empty_or_non_string_event_id_is_malformed(tmp_path, con, vault):
    # Spec step 1 "... or has no event_id"; D15: missing, null, empty or non-string.
    no_id = json.loads(ev())
    del no_id["event_id"]
    d, p = write_events(tmp_path, [json.dumps(no_id), ev(""), ev(None), ev(123)])
    stats = ingest(con, vault, d, p)
    assert stats["malformed"] == 4 and stats["lines_total"] == 4 and events(con) == {}


# 4 ---------------------------------------------------------------------------------------------
def test_duplicate_event_id_in_same_file(tmp_path, con, vault):
    # Spec step 2 "Duplicate: an event_id already seen -> count it and skip it". First one kept.
    d, p = write_events(tmp_path, [ev("evt_1", call_id="first"), ev("evt_1", call_id="second")])
    stats = ingest(con, vault, d, p)
    assert stats["stored"] == 1 and stats["duplicates"] == 1
    assert events(con)["evt_1"]["call_id"] == "first"


# 5 ---------------------------------------------------------------------------------------------
def test_duplicate_event_id_in_later_drop(tmp_path, con, vault):
    # Spec step 2 "... already seen (in any drop)".
    d1, p1 = write_events(tmp_path, [ev("evt_1")], drop="drop_1")
    d2, p2 = write_events(tmp_path, [ev("evt_1"), ev("evt_2")], drop="drop_2")
    ingest(con, vault, d1, p1)
    stats = ingest(con, vault, d2, p2)
    assert stats["duplicates"] == 1 and stats["stored"] == 1
    assert events(con)["evt_1"]["source_file"] == "drop_1/events/events_001.jsonl"


# 6 ---------------------------------------------------------------------------------------------
def test_lines_total_counts_malformed_and_duplicates_skips_empty_counts_whitespace(tmp_path, con, vault):
    # Spec: "events_total counts every non-empty line read, including malformed and duplicate lines".
    # D25: a whitespace-only line is non-empty -> counted, and malformed.
    d, p = write_events(tmp_path, [ev("evt_1"), "", ev("evt_1"), "{bad", "   \t", "", ev("evt_2")])
    stats = ingest(con, vault, d, p)
    assert stats["lines_total"] == 5
    assert stats["malformed"] == 2 and stats["duplicates"] == 1 and stats["stored"] == 2
    # source_line is the physical line number (empty lines included)
    assert events(con)["evt_2"]["source_line"] == 7
    row = con.execute("SELECT rows_total, malformed, duplicates FROM ingested_files").fetchone()
    assert tuple(row) == (5, 2, 1)


# 7 ---------------------------------------------------------------------------------------------
def test_crlf_and_lone_cr_line_endings_create_no_malformed_lines(tmp_path, con, vault):
    # D25/D41: only the terminator is stripped; \r\n and \r never leave junk lines behind.
    raw = (ev("evt_1") + "\r\n" + ev("evt_2") + "\r\n\r\n" + ev("evt_3") + "\r" + ev("evt_4") + "\r\n")
    d, p = write_events(tmp_path, raw.encode("utf-8"))
    stats = ingest(con, vault, d, p)
    assert stats["malformed"] == 0 and stats["lines_total"] == 4 and stats["stored"] == 4


# 8 ---------------------------------------------------------------------------------------------
def test_override_without_phone_and_invalid_phone_are_stored_unlinked(tmp_path, con, vault):
    # Spec: "disposition.override ... Has no phone, only call_id". D16: missing/invalid phone is not
    # malformed; the event is stored and classified (orphan or linked) at report time.
    override = json.loads(ev("evt_o", event_type="disposition.override",
                             payload={"disposition_code": "X", "overridden_by": "sup", "reason": "r"}))
    del override["phone"]
    d, p = write_events(tmp_path, [json.dumps(override), ev("evt_bad", phone="N/A")])
    stats = ingest(con, vault, d, p)
    assert stats["malformed"] == 0 and stats["stored"] == 2
    rows = events(con)
    assert rows["evt_o"]["phone_bidx"] is None and rows["evt_o"]["phone_enc"] is None
    assert rows["evt_bad"]["phone_bidx"] is None
    assert vault.decrypt(rows["evt_bad"]["phone_enc"]) == "N/A"      # audit trail, encrypted


# 9 ---------------------------------------------------------------------------------------------
def test_three_timestamp_forms_of_same_instant_store_identically(tmp_path, con, vault):
    # Spec "Timestamps": UTC (...Z), offset (...+05:30), naive (= IST). D42: fixed-width UTC ISO.
    d, p = write_events(tmp_path, [ev("e_z", occurred_at="2026-10-03T04:31:00Z"),
                                   ev("e_off", occurred_at="2026-10-03T10:01:00+05:30"),
                                   ev("e_naive", occurred_at="2026-10-03 10:01:00")])
    ingest(con, vault, d, p)
    got = {k: v["occurred_at"] for k, v in events(con).items()}
    assert set(got.values()) == {"2026-10-03T04:31:00.000000+00:00"}


# 10 --------------------------------------------------------------------------------------------
def test_unparseable_occurred_at_is_unusable_not_malformed(tmp_path, con, vault):
    # D17: skipped, counted as unusable_timestamp, still counted in lines_total, not malformed.
    no_ts = json.loads(ev("evt_3"))
    del no_ts["occurred_at"]
    d, p = write_events(tmp_path, [ev("evt_1"), ev("evt_2", occurred_at="yesterday"), json.dumps(no_ts)])
    stats = ingest(con, vault, d, p)
    assert stats["unusable_timestamp"] == 2 and stats["malformed"] == 0 and stats["lines_total"] == 3
    assert set(events(con)) == {"evt_1"}
    assert con.execute("SELECT unusable_timestamp FROM ingested_files").fetchone()[0] == 2


# 11 --------------------------------------------------------------------------------------------
def test_payload_allowlist(tmp_path, con, vault):
    # CLAUDE.md / D28: only allowlisted payload keys are stored; PII like customer_name never is.
    d, p = write_events(tmp_path, [ev("evt_1", event_type="call.ended",
                                      payload={"duration_sec": 42, "end_reason": "hangup",
                                               "customer_name": "Zephyrine Quillfeather"})])
    ingest(con, vault, d, p)
    assert json.loads(events(con)["evt_1"]["payload_json"]) == {"duration_sec": 42, "end_reason": "hangup"}


# 12 --------------------------------------------------------------------------------------------
def test_reingest_same_file_skips_and_changed_bytes_refused(tmp_path, con, vault):
    # D3: same identity + same bytes -> skip; same identity + different bytes -> refuse, DB unchanged.
    d, p = write_events(tmp_path, [ev("evt_1"), "{bad"])
    ingest(con, vault, d, p)
    before = snapshot(con)
    assert ingest(con, vault, d, p)["skipped"] is True
    assert snapshot(con) == before
    write_events(tmp_path, [ev("evt_1"), ev("evt_2")])
    with pytest.raises(SystemExit, match="different content"):
        ingest(con, vault, d, p)
    assert snapshot(con) == before


# 13 --------------------------------------------------------------------------------------------
def test_crash_mid_file_rolls_back_everything(tmp_path, con, vault, monkeypatch):
    # D3: one transaction per file -> no events and no ingested_files row after a crash.
    d, p = write_events(tmp_path, [ev("evt_1"), ev("evt_2"), ev("evt_3")])
    real = ingest_events.normalize_phone
    calls = {"n": 0}

    def flaky(raw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated crash")
        return real(raw)

    monkeypatch.setattr(ingest_events, "normalize_phone", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ingest(con, vault, d, p)
    assert all(rows == [] for rows in snapshot(con).values())
    monkeypatch.setattr(ingest_events, "normalize_phone", real)
    assert ingest(con, vault, d, p)["stored"] == 3                     # clean re-run succeeds


# 14 --------------------------------------------------------------------------------------------
def test_db_file_contains_no_plaintext_phone(tmp_path, vault):
    # CLAUDE.md PII rule: phones only as AES-GCM (phone_enc) + keyed blind index.
    db_path = tmp_path / "t.db"
    con = connect(str(db_path), vault)
    d, p = write_events(tmp_path, [ev("evt_1", phone="98765-43210"), ev("evt_2", phone="+917012345678"),
                                   ev("evt_3", phone="N/A 8123456789 x")])
    ingest(con, vault, d, p)
    con.close()
    blob = db_path.read_bytes()
    for needle in ("9876543210", "+919876543210", "98765-43210", "7012345678", "8123456789"):
        assert needle.encode("utf-8") not in blob, needle


# 15 --------------------------------------------------------------------------------------------
def test_missing_event_type_is_stored_as_empty_not_duplicate(tmp_path, con, vault):
    # D39: event_type missing/non-string -> "" (well-formed); must not be miscounted as duplicate
    # through INSERT OR IGNORE swallowing a NOT NULL violation.
    no_type = json.loads(ev("evt_1"))
    del no_type["event_type"]
    d, p = write_events(tmp_path, [json.dumps(no_type), ev("evt_2", event_type=7)])
    stats = ingest(con, vault, d, p)
    assert stats["duplicates"] == 0 and stats["stored"] == 2
    assert {r["event_type"] for r in events(con).values()} == {""}


# 16 --------------------------------------------------------------------------------------------
def test_integer_phone_accepted_other_non_strings_are_null(tmp_path, con, vault):
    # D40: JSON integer phone -> its decimal text; other non-string identity fields -> NULL.
    d, p = write_events(tmp_path, [ev("evt_1", phone=9876543210),
                                   ev("evt_2", workflow_id=5, call_id=["x"], phone=True)])
    ingest(con, vault, d, p)
    rows = events(con)
    assert rows["evt_1"]["phone_bidx"] == vault.blind_index("+919876543210")
    assert rows["evt_2"]["workflow_id"] is None and rows["evt_2"]["call_id"] is None
    assert rows["evt_2"]["phone_bidx"] is None and rows["evt_2"]["phone_enc"] is None


# 17 --------------------------------------------------------------------------------------------
def test_bom_tolerated_and_invalid_utf8_line_is_malformed(tmp_path, con, vault):
    # D41: leading BOM stripped; a line that is not valid UTF-8 is not valid JSON -> malformed,
    # and it never fails the rest of the file.
    raw = b"\xef\xbb\xbf" + ev("evt_1").encode() + b"\n" + b'{"event_id": "\xff\xfe"}\n' + ev("evt_2").encode()
    d, p = write_events(tmp_path, raw)
    stats = ingest(con, vault, d, p)
    assert stats["lines_total"] == 3 and stats["malformed"] == 1 and stats["stored"] == 2
