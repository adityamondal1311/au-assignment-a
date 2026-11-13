"""Clean-room verification harness.

    python -m solution.verify --seeds 42 7 1234

For each seed, in a fresh temp folder with fresh databases:
  1. generate data with the official generator
  2. DB "A": ingest drop_1, ingest drop_2, report               -> reports_incremental
  3. DB "A": ingest drop_1 and drop_2 AGAIN, report             -> must be byte-identical (idempotency)
  4. DB "B": ingest drop_1 + drop_2 in ONE command, report      -> must equal step 2 (incremental == all-at-once)
  5. internal consistency checks across the five files
  6. PII scan: no phone number or customer name from the input appears in the DB file or reports

We have no official expected output, so these invariants are the strongest check available.
Exit code 0 = all good."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GENERATOR = REPO / "assignment-a-data" / "tools" / "generate_data.py"
FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]
HEADERS = {
    "batch_summary.csv": "workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,"
                         "in_progress_dialed,completed",
    "daily_overview.csv": "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads",
    "lead_report.csv": "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,"
                       "last_call_at,current_disposition,ever_success",
    "rejections.csv": "workflow_id,batch_id,row_number,reason",
}
DQ_KEYS = ["lead_rows_total", "lead_rows_rejected", "rejections_by_reason", "duplicate_lead_rows_merged",
           "field_warnings", "events_total", "malformed_events", "duplicate_events_dropped", "orphan_events",
           "unknown_disposition_events"]

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def run(args: list[str], db: Path, cwd: Path) -> None:
    env = {**os.environ, "AU_DB": str(db), "PYTHONPATH": str(REPO)}
    res = subprocess.run([sys.executable, "-m", "solution", *args, "--db", str(db)],
                         cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        print(res.stdout, res.stderr, sep="\n")
        raise SystemExit(f"command failed: solution {' '.join(args)}")


def read_reports(folder: Path) -> dict[str, bytes]:
    return {f: (folder / f).read_bytes() for f in FILES}


def rows(folder: Path, name: str) -> list[dict]:
    with open(folder / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def consistency(folder: Path) -> None:
    for name, header in HEADERS.items():
        first = (folder / name).read_text(encoding="utf-8").splitlines()[0]
        check(first == header, f"{name}: exact header")
    bs, do, lr, rj = (rows(folder, n) for n in ["batch_summary.csv", "daily_overview.csv",
                                                 "lead_report.csv", "rejections.csv"])
    dq = json.loads((folder / "data_quality.json").read_text(encoding="utf-8"))

    check(list(dq.keys()) == DQ_KEYS, "data_quality.json: exact keys in contract order")
    check(sum(int(r["total_leads"]) for r in bs) == len(lr), "sum(batch_summary.total_leads) == lead_report rows")
    check(all(int(r["total_leads"]) == sum(int(r[k]) for k in ["yet_to_reach_out", "in_progress_telephony_failed",
          "in_progress_dialed", "completed"]) for r in bs), "every batch: buckets add up to total_leads")
    check(dq["lead_rows_total"] == len(lr) + dq["lead_rows_rejected"] + dq["duplicate_lead_rows_merged"],
          "lead_rows_total == leads + rejected + merged")
    check(len(rj) == dq["lead_rows_rejected"] == sum(dq["rejections_by_reason"].values()),
          "rejections.csv rows == lead_rows_rejected == sum(rejections_by_reason)")
    check(sum(int(r["total_calls"]) for r in do) == sum(int(r["total_calls"]) for r in lr),
          "daily total_calls == lead_report total_calls")
    check(sum(int(r["connected_calls"]) for r in do) == sum(int(r["connected_calls"]) for r in lr),
          "daily connected_calls == lead_report connected_calls")
    check(all(re.fullmatch(r"\d\.\d{4}", r["connectivity_rate"]) for r in do), "connectivity_rate has 4 decimals")
    check(all(r["ever_success"] in ("true", "false") for r in lr), "booleans are lowercase true/false")
    check(all(re.fullmatch(r"\+91\*{6}\d{4}", r["phone_masked"]) for r in lr), "phone_masked format")
    check(all(r["last_call_at"] == "" or r["last_call_at"].endswith("+05:30") for r in lr), "last_call_at in IST")
    check(all((r["bucket"] == "yet_to_reach_out") == (r["total_calls"] == "0") for r in lr),
          "yet_to_reach_out <=> zero calls")
    for name, data, key in [("batch_summary", bs, lambda r: (r["workflow_id"], r["batch_id"])),
                            ("daily_overview", do, lambda r: (r["workflow_id"], r["date"])),
                            ("lead_report", lr, lambda r: (r["workflow_id"], r["external_ref"])),
                            ("rejections", rj, lambda r: (r["workflow_id"], r["batch_id"], int(r["row_number"])))]:
        check([key(r) for r in data] == sorted(key(r) for r in data), f"{name}: sorted as the contract says")


def _ten_digits(cell: str) -> str | None:
    # Independent of solution.normalize on purpose: the scan must not inherit a bug from the code it checks.
    d = re.sub(r"\D", "", cell)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if len(d) == 10 and d[0] in "6789" else None


def pii_scan(data_dir: Path, targets: list[Path]) -> None:
    phones, names = set(), set()
    for f in data_dir.glob("drop_*/leads/*/*.csv"):
        with open(f, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader)                                   # skip header ("Policy No" is not a name)
            for r in reader:
                for cell in r:
                    if (p := _ten_digits(cell)):
                        phones.add(p)
                    if re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", cell.strip()):
                        names.add(cell.strip())
    for t in targets:
        blob = t.read_bytes()
        leaked_p = [p for p in phones if p.encode() in blob]
        leaked_n = [n for n in names if n.encode() in blob]
        check(not leaked_p and not leaked_n,
              f"no plaintext PII in {t.name} (checked {len(phones)} phones, {len(names)} names)"
              + (f" LEAKED e.g. {(leaked_p + leaked_n)[:3]}" if leaked_p or leaked_n else ""))


def verify_seed(seed: int) -> None:
    print(f"\n=== seed {seed} ===")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        subprocess.run([sys.executable, str(GENERATOR), "--seed", str(seed), "--scale", "small",
                        "--out", str(t / "data")], check=True, capture_output=True)
        d1, d2 = str(t / "data" / "drop_1"), str(t / "data" / "drop_2")
        db_a, db_b = t / "a.db", t / "b.db"

        run(["ingest", d1], db_a, REPO)
        run(["ingest", d2], db_a, REPO)
        run(["report", "--out", str(t / "inc")], db_a, REPO)
        run(["ingest", d1], db_a, REPO)
        run(["ingest", d2], db_a, REPO)
        run(["report", "--out", str(t / "again")], db_a, REPO)
        run(["ingest", d1, d2], db_b, REPO)
        run(["report", "--out", str(t / "once")], db_b, REPO)

        inc, again, once = (read_reports(t / n) for n in ["inc", "again", "once"])
        for f in FILES:
            check(inc[f] == again[f], f"idempotent re-ingest: {f} unchanged")
            check(inc[f] == once[f], f"incremental == all-at-once: {f}")
        consistency(t / "inc")
        pii_scan(t / "data", [db_a, *[(t / "inc" / f) for f in FILES]])
        dq = json.loads((t / "inc" / "data_quality.json").read_text(encoding="utf-8"))
        print("  data_quality:", json.dumps(dq))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, nargs="+", default=[42, 7, 1234])
    for seed in ap.parse_args().seeds:
        verify_seed(seed)
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()