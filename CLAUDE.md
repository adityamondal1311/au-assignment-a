# Agentic Universe — Assignment A: Interaction Ledger & Reporting Pipeline

## Source of truth
- `assignment-a-data/README.md` (the spec) and `docs/GLOSSARY.md`. Spec rules beat "sensible" behaviour.
- When the spec is ambiguous: do NOT decide silently. Propose options, I decide, it goes into
  `docs/decisions/decision-log.md`.

## Hard constraints
- All my code lives in `solution/`. Never modify original files: anything under `assignment-a-data/`,
  `assignment-b-workflow/`, `docs/GLOSSARY.md`, root `README.md`, `.gitignore`, `docker-compose.yml`.
  (`SUBMISSION.md`, `AI_USAGE.md` are templates I fill in; `requirements.txt` I only append to.)
- Python 3.11+, standard library + `cryptography` + `pytest`. Ask before adding any dependency.
- Never commit or print: generated data (`data*/`), databases (`*.db`), `.env`, keys.
- Always open text files with `encoding="utf-8"` (data contains ₹; Windows default is not UTF-8)
  and CSVs with `newline=""`.

## Architecture (decided — do not change without asking)
- SQLite via the standard `sqlite3` module. DDL lives in `solution/schema.sql`. No ORM.
- Ingest (write side) and report (read side) are separate.
- `events` is an append-only ledger (DB triggers forbid UPDATE/DELETE). Raw JSON lines are never stored.
- Event→lead linking happens at REPORT time, from scratch, every run. No "pending/reconcile" updates.
- Idempotency: `ingested_files` keyed by logical identity `<drop folder>/<path inside drop>` + SHA-256:
  same identity + same bytes = skip; same identity + different bytes = refuse. One transaction per file
  (rows + counters + file record commit together).
- PII (`"pii": true` fields): AES-256-GCM encrypted (`pii_enc`). Phones matched via
  HMAC-SHA256 blind index with a separate key (`phone_bidx`). Keys from env or `.env`; created only when
  BOTH are absent; one missing/invalid → refuse. Fingerprint over BOTH keys in `meta`; refuse on mismatch.
- Event payloads are stored via an allowlist (`disposition_code`, `overridden_by`, `reason`,
  `duration_sec`); raw JSON lines are never stored. Never print names, phones or raw rows.
- Schema-driven: lead ingestion reads field definitions from `assignment-a-data/schemas/*.json`;
  no workflow names hard-coded in logic.
- Pure functions for parsing (`normalize.py`) and derived facts (`facts.py`); DB code stays thin.

## Module map
- `solution/normalize.py`   header/phone/date/decimal/enum/timestamp parsing, masking (pure)
- `solution/crypto.py`      key loading, AES-GCM encrypt/decrypt, blind index (Vault)
- `solution/schema.sql`     DDL + append-only triggers
- `solution/db.py`          connect, init schema, key fingerprint check, transaction helper
- `solution/config.py`      paths, schema + disposition catalogue loading
- `solution/ingest_leads.py`, `solution/ingest_events.py`   write side
- `solution/facts.py`       linking, calls, effective disposition, buckets (pure)
- `solution/reports.py`     the five output files
- `solution/cli.py`, `solution/__main__.py`   `python -m solution ingest|report`
- `solution/verify.py`      clean-room verification harness
- `solution/tests/`         pytest

## Commands
- Tests: `python -m pytest -q`
- Generate data: `python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data`
- Ingest: `python -m solution ingest data/drop_1 [more paths...] [--db FILE]`
- Report: `python -m solution report --out reports [--db FILE]`
- Verify: `python -m solution.verify --seeds 42 7 1234`
- DB default path: env `AU_DB`, else `au_ledger.db` in the current directory.

## How to work with me
1. Inspect before modifying. 
2. Explain the plan and the files you'll touch before editing.
3. One bounded increment at a time; stop and ask if it needs an architecture change.
4. Every spec rule you implement gets a test whose name or comment cites the rule.
5. Run `python -m pytest -q` and paste the real output. Never claim tests pass without running them.
6. Never weaken, delete or skip a test to make the suite pass.
7. Show the diff summary and explain the key logic at the end of each increment.
8. Do not `git push`. I review and push myself.