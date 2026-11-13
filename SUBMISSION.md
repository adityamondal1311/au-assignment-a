# Submission

## Candidate
- **Name:** Aditya Mondal
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~9 hours

## How to run
Run all commands from the repo root. Python 3.11+.

Windows (PowerShell):
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```
macOS / Linux:
```bash
python3 -m venv .venv
source .venv/bin/activate
```
Then, on any OS:
```bash
pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports
python -m pytest -q
python -m solution.verify --seeds 42 7 1234     # optional self-check (see Testing)
```
- The first run creates `.env` with two random keys (git-ignored). Keep it together with the DB: the
  program refuses a DB that was created with different keys. To start over, delete `au_ledger.db`.
- Choose the DB file with `--db FILE` or env `AU_DB` (default `au_ledger.db`).
- `ingest` accepts several drops (processed in the order given) or a root folder (`ingest data` expands
  to its `drop_*` folders sorted by name).
- Exit codes: 0 ok, 1 refused (changed file, wrong keys, schema version, unknown workflow), 2 usage
  error.

## Design
```
 data/drop_N/leads/<wf>/*.csv ─┐
 data/drop_N/events/*.jsonl  ──┤
                               ▼
   pre-check: every file's identity + SHA-256 (refuse before any write)
                               ▼
   ingest_leads / ingest_events   (one transaction per file)
                               ▼
   SQLite: meta · ingested_files · leads · rejections · events (append-only ledger)
                               ▼
   report: load all leads + all events → facts.compute_facts (from scratch, pure) → 5 files
```
- **Write side and read side are separate.** Ingest validates, normalises and stores. Report derives
  everything from scratch on every run.
- **Tables** (`solution/schema.sql`, stdlib `sqlite3`, no ORM):
  - `meta`: schema version + key fingerprint. Refuses a DB built by other code or with other keys.
  - `ingested_files`: one row per ingested file (logical identity, SHA-256, per-file counters).
    Makes re-ingest a no-op, and `data_quality.json` is rebuilt by summing these counters.
  - `leads`: current state per `(workflow_id, phone blind index)`. Mutable on purpose, because
    duplicates merge into it. `batch_id` is set by the first row and never changes.
  - `rejections`: `(source_key, row_number) → reason`. No PII by construction.
  - `events`: the interaction ledger. Every well-formed, non-duplicate event, append-only.
- **Report-time linking (D2, D27).** Events are never linked or updated at ingest. "Quarantine" is
  a derived view: the append-only ledger keeps every event, and orphan status is recomputed at report
  time from the full ledger. An orphan from `drop_1` counts as soon as its lead arrives in `drop_2`,
  and incremental runs equal all-at-once runs by construction. Every tie uses an explicit sort key,
  never DB order (D12, D13).
- **Idempotency (D3, D7, D36):**
  - Each file has a logical identity `<drop folder>/<path inside drop>` plus its SHA-256.
  - Same identity + same bytes → skip. Same identity + different bytes → refuse.
  - Before writing anything, every file in every given drop is checked.
  - Rows, counters and the file record commit in one transaction per file.
- **PII (D4, D5, D28, D29):**
  - Fields marked `"pii": true` are AES-256-GCM encrypted (random nonce).
  - Phones are matched via an HMAC-SHA256 blind index with a separate key.
  - A fingerprint over both keys is stored in `meta`; a mismatch is refused.
  - Event payloads are stored through an allowlist; raw lines are never stored.
  - Reports decrypt only to mask the phone.
  - Known limitation: the blind index leaks equality (same phone → same index value).
- **Append-only:** triggers abort UPDATE/DELETE on `events`. `PRAGMA recursive_triggers = ON` makes
  `INSERT OR REPLACE` hit the DELETE trigger too (D32).

## Decisions & assumptions
Full text in `docs/decisions/decision-log.md`.

| Decision | Why | Alternative considered |
|---|---|---|
| D1: SQLite + stdlib `sqlite3`, DDL in `schema.sql` | Zero setup from a clean clone; transactions + unique constraints suffice | Postgres; SQLAlchemy |
| D2: Link events to leads at report time | Ledger stays append-only; late leads resolve orphans; incremental == all-at-once | Store link + reconcile |
| D3: Idempotency via logical file identity + SHA-256; changed bytes refused; one txn per file | Re-ingest is a no-op; crash rolls back; no double counting | Hash-only key; row-level dedupe only |
| D4: AES-256-GCM storage + HMAC-SHA256 blind index (separate key) | Random-nonce encryption leaks nothing; keyed HMAC not brute-forceable | Plain SHA-256; deterministic encryption |
| D5: Keys from env or git-ignored `.env`, created only if both absent; fingerprint in DB | Works from a clean clone; prevents silent key mismatch | Manual key setup |
| D6: `connectivity_rate` via `f"{x:.4f}"` (0.65625 → `0.6562`) | Most likely what a Python reference uses | Decimal ROUND_HALF_UP → `0.6563` |
| D7: Identity = drop folder name + path inside the drop | Same drop from another directory is still recognised | Absolute path |
| D8: Non-empty phone with no usable digits (`N/A`) → `invalid_phone` | Not empty, so "present but invalid" | `missing_required:phone` |
| D9: Drop `91` only at exactly 12 digits (`0` only at 11) | Rule is length-conditioned; valid numbers may start with 91 | Strip any `91` prefix |
| D10: `rejections_by_reason` lists observed reasons only, keys sorted | Contract keys read as illustrative | Every possible reason with 0 |
| D11: A merged duplicate overwrites the reference field like any field | Spec: non-null fields, last write wins, no exception | Freeze `external_ref` |
| D12: `lead_report` ties broken by canonical phone | Deterministic, independent of ingest grouping | Lead creation order |
| D13: Event ties → higher `event_id`; call ties → higher `call_id` | Must not depend on ingestion order | Insertion order / rowid |
| D14: Override links via `(workflow_id, call_id)` to its `call.initiated`'s lead; else orphan | A call exists only via its `call.initiated` | Any linked sibling event |
| D15: Valid JSON that is not an object, or bad/missing `event_id` → malformed | "No `event_id`" = no usable `event_id` | Treat as normal event |
| D16: Well-formed event missing `workflow_id`/`phone`/`call_id` → stored, orphan at report | Malformed is defined narrowly | Count as malformed |
| D17: Unparseable `occurred_at` → skipped, `unusable_timestamp` counter (ingest summary only) | Facts need a time; contract keys are fixed | Fail file; null time; malformed |
| D18: Drop order = CLI order; root folder expands to `drop_*` sorted | Spec: process drops in the order ingested | Always sort by name |
| D19: Blank row = every cell (incl. unmapped) empty | Spec wording is literal | Mapped columns only |
| D20: Two columns map to one field → leftmost wins, logged | Deterministic, never fails a file | Refuse the file |
| D21: Disposition codes matched exactly (case-sensitive) | Case-insensitivity is specified for enums only | Case-insensitive |
| D22: Orphan/unknown computed at report time; other counters per file at ingest | Orphan counts must reflect the final state | Count at ingest |
| D23: Decimal: strip all commas, `Decimal`, ROUND_HALF_UP to 2 places | No grouping rule given; half-up is money convention | Strict grouping; banker's rounding |
| D24: Dates: strict zero-padded forms of the four listed formats | Closest to spec; generator zero-pads | Non-padded / full month names |
| D25: Whitespace-only event line is non-empty → counted + malformed | Spec counts every non-empty line | Treat as empty |
| D26: Lead CSVs read as `utf-8-sig` | Tolerates Excel BOM | Plain `utf-8` |
| D27: Ledger keeps all well-formed events; quarantine is a derived view | Avoids mutable state (D2); stated here for reviewers | Physical quarantine table |
| D28: `end_reason` added to the payload allowlist | In the spec's payload table, not PII; unrecoverable otherwise | Original four fields |
| D29: `phone_masked` from decrypting `pii_enc`; no last-4 column | No extra partial PII at rest | Store `last4` plaintext |
| D30: ASCII digits only, `fullmatch`, high-precision decimals | Unicode digits corrupt phones; long amounts crashed a file | Python defaults |
| D31: `parse_timestamp` uses `fromisoformat` (accepts extra ISO shapes) | Generator emits only the 3 spec shapes; simple | Three strict `strptime` formats |
| D32: `PRAGMA recursive_triggers = ON` blocks INSERT OR REPLACE | REPLACE skips DELETE triggers by default; BEFORE INSERT would break duplicate counting | BEFORE INSERT trigger |
| D33: Rejections keyed by `(source_key, row_number)` | Same batch name in two drops must not collide | `(workflow_id, batch_id, row_number)` |
| D34: Schema version checked on connect; mismatch refused | `CREATE IF NOT EXISTS` never alters a table | Migrations |
| D35: `.env` read as `utf-8-sig`, anchored to repo root, newline-safe append | BOM / missing newline corrupted key loading | CWD-relative plain utf-8 |
| D36: Every file in every given drop checked before ingesting any | A late changed file would leave a drop partly ingested | Per-file check only |
| D37: Required non-phone field present but unparseable → `missing_required:<field>` | Only two reasons defined; unusable = missing | New `invalid:<field>` reason |
| D38: Defaults not stored; applied on read to fields still null | Spec: defaults apply after merging | Write defaults at ingest |
| D39: Missing/non-string `event_type` stored as `''`, ignored at report | Keeps `INSERT OR IGNORE` meaning "duplicate" only | Malformed |
| D40: Integer JSON phone → its text; other non-string identity fields → missing | One odd value must not roll back a file | All non-strings missing |
| D41: Events split on raw bytes (`\n`, `\r\n`, `\r`), BOM stripped, per-line decode | One bad byte must not fail the file | Whole-file strict decode |
| D42: `occurred_at` stored as fixed-width UTC microsecond ISO | String order == time order; no precision loss | Seconds / variable width |
| D43: Several `call.initiated` for one call → counted once; earliest defines time/date | Re-sent events must not double count | Latest; count each |
| D44: Conflicting initiated leads for one call → override follows the earliest | Consistent with D43; deterministic | Orphan it; apply to all |
| D45: Linked event without `call_id` is not an orphan but belongs to no call | It linked by phone; calls are keyed by `call_id` | Count as orphan |
| D46: Linked `disposition.*` with missing/non-string code → unknown disposition | Literally "not in `dispositions.csv`" | Ignore silently |
| D47: Exit codes 0 ok / 1 refused / 2 usage error | Callers can tell misuse from refused state | Exit 1 for everything |
| D48: `report` on a missing DB file refuses (exit 2) | All-zero reports would hide a wrong `--db` | Create empty DB |
| D49: Only `leads/<wf>/*.csv` and `events/*.jsonl` ingested; others ignored | Matches generator layout; stray files harmless | Warn on unexpected files |
| D50: Pre-check refuses a lead folder with no schema before any write | Avoids a partly ingested drop | Refuse when reached |
| D51: `rejections.csv` ties broken by `reason` | Deterministic, independent of ingest order | DB / insertion order |

## Failure modes considered
- **Dirty lead rows:** header aliases/odd spacing, BOM, blank rows, bad phones, missing required fields
  → rejected with a single reason; never crash the file (D8, D19, D26, D37).
- **Invalid optional values:** stored null + one field warning (only for non-rejected rows).
- **Malformed / duplicate events:** bad JSON, undecodable bytes, no `event_id` → malformed;
  repeated `event_id` in any drop → duplicate via `UNIQUE` + `INSERT OR IGNORE` (D15, D41).
- **Re-runs:** same file + same bytes → skipped; reports byte-identical (D3).
- **Crash mid-file:** one transaction per file → rolled back; re-run starts clean.
- **Changed file at the same path:** refused before any row of the command is written (D36).
- **Late leads:** linking at report time turns earlier orphans into linked events (D2).
- **Wrong / partial keys:** fingerprint mismatch or only one key → refused, never regenerated (D5).
- **Schema version mismatch:** refused with instructions (D34).
- **Encoding / BOM:** all text I/O is explicit UTF-8; BOMs tolerated in CSV, JSONL and `.env`
  (D26, D35, D41).
- **Not handled:**
  - concurrent ingest processes (no lock)
  - key rotation
  - right-to-be-forgotten (deleting a person's data)
  - drops ingested out of order change merge winners / `batch_id` (by spec, D18)
  - performance at large scale not measured

## Testing
- `python -m pytest -q`: **193 passed** (pytest-collected cases per module):

  | Module | Tests |
  |---|---|
  | `test_normalize.py` | 63 |
  | `test_facts.py` | 45 |
  | `test_ingest_leads.py` | 18 |
  | `test_ingest_events.py` | 17 |
  | `test_config.py` | 11 |
  | `test_cli.py` | 10 |
  | `test_reports.py` | 10 |
  | `test_db.py` | 8 |
  | `test_keys.py` | 7 |
  | `test_crypto_schema.py` | 4 |
- Each implemented spec rule has a test whose name or comment cites the rule or decision.
- `python -m solution.verify --seeds 42 7 1234`: **all checks pass on all three seeds**. For each seed, in a
  fresh temp folder, it checks:
  - re-ingest leaves reports byte-identical
  - incremental == all-at-once (byte-identical)
  - exact headers and contract sort orders
  - cross-file invariants (bucket sums, call totals, lead row accounting)
  - a plaintext PII scan of the DB and the reports
- Not tested: no official expected output exists, so correctness rests on the spec tests and invariants.
  Large-scale data was not run.

## Results
- **Seed 42 `data_quality.json`:**
  - lead rows: total 2057, rejected 108, duplicates merged 57, field warnings 122
  - rejections by reason: `invalid_phone` 42, `missing_required:customer_name` 25,
    `missing_required:order_id` 13, `missing_required:phone` 14, `missing_required:policy_number` 14
  - events: total 8745, malformed 8, duplicates dropped 237
  - orphan events 462, unknown dispositions 18
- **Seed 42 orphans:** 1123 after `drop_1` → 462 after `drop_2` (late leads linked at report time).
- **Seeds 7 / 1234 (verify run):** orphan_events 479 / 474, unknown_disposition_events 11 / 15.
- **Timing:** report run time 0.34 seconds (seed 42, small scale).
- **Surprising in the data:**
  - Seed 99 contains a day with 84 connected of 128 calls (0.65625). That is exactly halfway between
    two 4-decimal values. We format with Python's `f'{x:.4f}'` (round-half-even → 0.6562), see D6.
  - Events for rejected leads stay orphans: the lead never exists, so they can never link.

## If I had another day
1. SQL-side aggregation for scale, plus large-dataset timing.
2. Incremental per-day materialisation.
3. Key rotation with key versions.
4. Postgres + advisory lock for concurrent ingest.
5. A stricter timestamp parser (D31).