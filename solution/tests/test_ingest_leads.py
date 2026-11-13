"""Lead ingestion — spec README "Rules (the spec)" -> "Lead files". Synthetic data only."""
import json
import os

import pytest

from solution import ingest_leads
from solution.config import load_schemas
from solution.crypto import Vault
from solution.db import connect
from solution.ingest_leads import effective_fields, ingest_lead_file

SCHEMAS = load_schemas()
HDR = "customer_name,phone,policy_number,policy_expiry,premium_amount,preferred_language\n"


@pytest.fixture
def vault():
    return Vault(os.urandom(32), os.urandom(32))


@pytest.fixture
def con(tmp_path, vault):
    c = connect(str(tmp_path / "t.db"), vault)
    yield c
    c.close()


def write_csv(tmp_path, text, wf="motor_renewal", name="batch_001.csv", drop="drop_1"):
    drop_dir = tmp_path / drop
    p = drop_dir / "leads" / wf / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return drop_dir, p


def ingest(con, vault, drop_dir, p):
    return ingest_lead_file(con, vault, SCHEMAS, drop_dir, p)


def leads(con, vault):
    """All leads with decrypted PII, keyed by canonical phone (test-only helper)."""
    out = {}
    for r in con.execute("SELECT * FROM leads"):
        pii = json.loads(vault.decrypt(r["pii_enc"]))
        out[(r["workflow_id"], pii["phone"])] = {
            "pii": pii, "fields": json.loads(r["fields_json"]),
            "external_ref": r["external_ref"], "batch_id": r["batch_id"]}
    return out


def rejections(con):
    return [(r["row_number"], r["reason"]) for r in
            con.execute("SELECT row_number, reason FROM rejections ORDER BY row_number")]


def snapshot(con):
    return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in ("leads", "rejections", "ingested_files")}


# 1 ---------------------------------------------------------------------------------------------
def test_header_aliases_reordered_and_unknown_column_ignored(tmp_path, con, vault):
    # Spec "Header matching": normalise (lower, trim, '_'/whitespace runs -> ' '), match name or
    # alias, ignore columns that match nothing.
    text = ("  Mobile_No ,Insured  Name,agent_notes,Policy No,PREMIUM,Language,expiry date\n"
            "98765 43210,Asha Rao,call after 6,POL-1,\"₹27,554\",HI,01-Dec-2026\n")
    d, p = write_csv(tmp_path, text)
    stats = ingest(con, vault, d, p)
    assert stats["rows_total"] == 1 and stats["rows_rejected"] == 0 and stats["field_warnings"] == 0
    lead = leads(con, vault)[("motor_renewal", "+919876543210")]
    assert lead["pii"] == {"customer_name": "Asha Rao", "phone": "+919876543210"}
    assert lead["fields"] == {"policy_number": "POL-1", "policy_expiry": "2026-12-01",
                              "premium_amount": "27554.00", "preferred_language": "hi"}
    assert lead["external_ref"] == "POL-1" and lead["batch_id"] == "batch_001"
    assert "agent_notes" not in lead["fields"]


# 2 ---------------------------------------------------------------------------------------------
def test_blank_rows_skipped_not_counted_and_row_numbers_skip_them(tmp_path, con, vault):
    # Spec "Blank rows ... skipped and not counted"; "Row numbers count non-blank data rows".
    # D19: a row with only an ignored column filled is NOT blank.
    text = ("customer_name,phone,policy_number,agent_notes\n"
            "A,9876543210,P1,\n"
            ",,,\n"
            "  ,\t, ,   \n"
            "\n"
            ",,,VIP\n"                       # not blank -> row 2, rejected
            ",123,P3,\n")                    # row 3
    d, p = write_csv(tmp_path, text)
    stats = ingest(con, vault, d, p)
    assert stats["rows_total"] == 3 and stats["rows_rejected"] == 2
    assert rejections(con) == [(2, "missing_required:customer_name"),
                               (3, "missing_required:customer_name")]


# 3 ---------------------------------------------------------------------------------------------
def test_first_failure_in_schema_order_wins(tmp_path, con, vault):
    # Spec "Rejection: walk the fields in schema order. The first failure becomes the reason."
    d, p = write_csv(tmp_path, HDR + ",12345,P1,,,\n")
    ingest(con, vault, d, p)
    assert rejections(con) == [(1, "missing_required:customer_name")]


# 4 ---------------------------------------------------------------------------------------------
def test_invalid_vs_missing_phone_and_missing_reference(tmp_path, con, vault):
    # D8: "N/A" is present-but-invalid -> invalid_phone; empty -> missing_required:phone.
    # Spec: required reference field empty -> missing_required:policy_number.
    text = HDR + ("A,N/A,P1,,,\n"
                  "B,  ,P2,,,\n"
                  "C,9876543210, ,,,\n")
    d, p = write_csv(tmp_path, text)
    stats = ingest(con, vault, d, p)
    assert stats["rows_rejected"] == 3
    assert rejections(con) == [(1, "invalid_phone"), (2, "missing_required:phone"),
                               (3, "missing_required:policy_number")]


# 5 ---------------------------------------------------------------------------------------------
def test_invalid_optional_fields_store_null_and_warn_but_not_on_rejected_rows(tmp_path, con, vault):
    # Spec "Optional field present but invalid -> store null and count one field warning.
    # Count warnings only for rows that are not rejected."
    text = HDR + ("A,9876543210,P1,31/02/2026,-5,fr\n"       # 3 warnings
                  ",9876543211,P2,31/02/2026,-5,fr\n")      # rejected: its 3 warnings not counted
    d, p = write_csv(tmp_path, text)
    stats = ingest(con, vault, d, p)
    assert stats["field_warnings"] == 3 and stats["rows_rejected"] == 1
    f = leads(con, vault)[("motor_renewal", "+919876543210")]["fields"]
    assert f["policy_expiry"] is None and f["premium_amount"] is None and f["preferred_language"] is None
    row = con.execute("SELECT field_warnings FROM ingested_files").fetchone()
    assert row["field_warnings"] == 3


# 6 ---------------------------------------------------------------------------------------------
def test_duplicate_in_later_file_merges_non_null_and_keeps_batch_id(tmp_path, con, vault):
    # Spec "Duplicates": first valid row creates the lead, its file stem is batch_id permanently;
    # later rows overwrite only non-null fields. Identity = canonical phone (format-independent).
    d, p1 = write_csv(tmp_path, HDR + "Asha,98765 43210,P1,2026-12-01,1000,ta\n")
    _, p2 = write_csv(tmp_path, HDR + "Asha Rao,+91-9876543210,P1-NEW,,2000,\n", name="batch_002.csv")
    ingest(con, vault, d, p1)
    stats = ingest(con, vault, d, p2)
    assert stats["rows_merged"] == 1 and stats["rows_total"] == 1
    all_leads = leads(con, vault)
    assert len(all_leads) == 1
    lead = all_leads[("motor_renewal", "+919876543210")]
    assert lead["batch_id"] == "batch_001"
    assert lead["pii"]["customer_name"] == "Asha Rao"
    assert lead["external_ref"] == "P1-NEW"                     # D11
    assert lead["fields"]["premium_amount"] == "2000.00"
    assert lead["fields"]["policy_expiry"] == "2026-12-01"       # null does not erase
    assert lead["fields"]["preferred_language"] == "ta"


# 7 ---------------------------------------------------------------------------------------------
def test_duplicate_inside_same_file_merges(tmp_path, con, vault):
    # Spec "Duplicates ... rows in file order": the second row in the same file merges.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,100,\n" "A,09876543210,P1,,200,\n")
    stats = ingest(con, vault, d, p)
    assert stats["rows_total"] == 2 and stats["rows_merged"] == 1
    assert con.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    assert leads(con, vault)[("motor_renewal", "+919876543210")]["fields"]["premium_amount"] == "200.00"


# 8 ---------------------------------------------------------------------------------------------
def test_same_phone_in_two_workflows_is_two_leads(tmp_path, con, vault):
    # Spec "Lead identity is (workflow_id, canonical phone)".
    d, p1 = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n")
    _, p2 = write_csv(tmp_path, "customer_name,phone,order_id\nA,9876543210,O1\n", wf="policy_feedback")
    ingest(con, vault, d, p1)
    stats = ingest(con, vault, d, p2)
    assert stats["rows_merged"] == 0
    assert set(leads(con, vault)) == {("motor_renewal", "+919876543210"),
                                      ("policy_feedback", "+919876543210")}


# 9 ---------------------------------------------------------------------------------------------
def test_reingesting_same_file_is_a_noop(tmp_path, con, vault):
    # D3: same logical identity + same bytes -> skip.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n,9876543211,P2,,,\n")
    ingest(con, vault, d, p)
    before = snapshot(con)
    assert ingest(con, vault, d, p) == {"skipped": True, "source_key": "drop_1/leads/motor_renewal/batch_001.csv"}
    assert snapshot(con) == before


# 10 --------------------------------------------------------------------------------------------
def test_same_source_key_with_changed_bytes_is_refused(tmp_path, con, vault):
    # D3: same identity + different bytes -> refuse, nothing written.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n")
    ingest(con, vault, d, p)
    before = snapshot(con)
    write_csv(tmp_path, HDR + "A,9876543210,P1,,,\nB,9876543299,P9,,,\n")
    with pytest.raises(SystemExit, match="different content"):
        ingest(con, vault, d, p)
    assert snapshot(con) == before


# 11 --------------------------------------------------------------------------------------------
def test_crash_mid_file_rolls_back_everything(tmp_path, con, vault, monkeypatch):
    # D3: one transaction per file -> a crash leaves no leads, rejections or file record.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n,9876543211,P2,,,\n"
                                     "C,9876543212,P3,,,\n")
    real, calls = ingest_leads.normalize_phone, []

    def flaky(raw):
        calls.append(raw)
        if len(calls) == 3:
            raise RuntimeError("simulated crash on row 3")
        return real(raw)

    monkeypatch.setattr(ingest_leads, "normalize_phone", flaky)
    with pytest.raises(RuntimeError, match="simulated crash"):
        ingest(con, vault, d, p)
    assert all(rows == [] for rows in snapshot(con).values())
    monkeypatch.setattr(ingest_leads, "normalize_phone", real)
    stats = ingest(con, vault, d, p)                             # clean re-run succeeds
    assert stats["rows_total"] == 3 and stats["rows_rejected"] == 1


# 12 --------------------------------------------------------------------------------------------
def test_db_file_contains_no_plaintext_phone_or_name(tmp_path, vault):
    # CLAUDE.md PII rule: names/phones only inside pii_enc (AES-GCM) + keyed blind index.
    db_path = tmp_path / "t.db"
    con = connect(str(db_path), vault)
    d, p = write_csv(tmp_path, HDR + "Zephyrine Quillfeather,98765 43210,P1,,,\n"
                                     "Ānandī Śrīvāstava,+91 7012345678,P2,,,\n")
    ingest(con, vault, d, p)
    con.close()
    blob = db_path.read_bytes()
    for needle in ("9876543210", "+919876543210", "7012345678", "Zephyrine", "Quillfeather",
                   "Ānandī", "Śrīvāstava"):
        assert needle.encode("utf-8") not in blob, needle


# 13 --------------------------------------------------------------------------------------------
def test_effective_fields_fills_default_only_when_null():
    # Spec "Defaults ... apply after merging, to fields that are still null" (D38: on read).
    schema = SCHEMAS["motor_renewal"]
    assert effective_fields({"preferred_language": None}, schema)["preferred_language"] == "en"
    assert effective_fields({}, schema)["preferred_language"] == "en"
    assert effective_fields({"preferred_language": "hi"}, schema)["preferred_language"] == "hi"
    stored = {"preferred_language": None}
    effective_fields(stored, schema)
    assert stored == {"preferred_language": None}               # input not mutated


def test_default_is_not_stored(tmp_path, con, vault):
    # D38: stored fields reflect what was observed; defaults applied on read only.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n")
    ingest(con, vault, d, p)
    assert leads(con, vault)[("motor_renewal", "+919876543210")]["fields"]["preferred_language"] is None


# 14 --------------------------------------------------------------------------------------------
def test_unknown_workflow_folder_is_refused(tmp_path, con, vault):
    # Schema-driven: workflow_id = folder name and must have a schema.
    d, p = write_csv(tmp_path, HDR + "A,9876543210,P1,,,\n", wf="no_such_workflow")
    with pytest.raises(SystemExit, match="Unknown workflow 'no_such_workflow'"):
        ingest(con, vault, d, p)
    assert all(rows == [] for rows in snapshot(con).values())


# extra -----------------------------------------------------------------------------------------
def test_duplicate_header_column_leftmost_wins(tmp_path, con, vault):
    # D20: two columns mapping to one field -> leftmost wins, collision reported (field name only).
    d, p = write_csv(tmp_path, "name,phone,customer name,policy\nFirst,9876543210,Second,P1\n")
    stats = ingest(con, vault, d, p)
    assert stats["header_collisions"] == ["customer_name"]
    assert leads(con, vault)[("motor_renewal", "+919876543210")]["pii"]["customer_name"] == "First"


def test_required_field_present_but_invalid_is_missing_required():
    # D37: a required non-phone value that fails to parse -> missing_required:<field>.
    schema = {"workflow_id": "x", "fields": [
        {"name": "phone", "type": "phone", "required": True},
        {"name": "when", "type": "date", "required": True}]}
    colmap, _ = ingest_leads._column_map(["phone", "when"], schema)
    _, reason, warnings = ingest_leads._parse_row(["9876543210", "31/02/2026"], colmap, schema)
    assert reason == "missing_required:when" and warnings == 0


def test_bom_in_header_is_tolerated(tmp_path, con, vault):
    # D26: utf-8-sig so an Excel BOM doesn't hide the first header.
    d, p = write_csv(tmp_path, "﻿" + HDR + "A,9876543210,P1,,,\n")
    assert ingest(con, vault, d, p)["rows_rejected"] == 0
