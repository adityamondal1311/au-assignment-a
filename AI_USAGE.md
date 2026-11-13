# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude (chat):** choosing the assignment, a design "playbook", reference code, independent seed-42
  cross-check numbers.
- **ChatGPT:** one review of the playbook.
- **Claude Code** (VS Code extension): spec analysis, review of the reference code, implementation and
  tests, driven by a `CLAUDE.md` with my rules: inspect first, plan before editing, one increment at a
  time, a test for every rule, paste real pytest output, never weaken tests, no push.

## What I used them for
- **12 Nov, Claude (chat):**
  - compared Assignment A vs B (I chose A)
  - produced a playbook: design, reference code for `normalize` / `crypto` / `schema` / `db` /
    `verify`, and seed-42 cross-check numbers to compare my pipeline against
- **13 Nov, ChatGPT:** reviewed the playbook. I adopted four of its points: file identity = path + hash,
  a key fingerprint over both keys, refusing a partial `.env`, and a payload allowlist. Claude (chat)
  then re-tested the updated reference code.
- **Claude Code, one increment per commit:**
  - **Recon (read-only):** spec analysis that listed 17 ambiguities (A1–A17) and 5 conflicts (C1–C5)
    with recommendations. My choices went into `docs/decisions/decision-log.md` (D8+).
  - **B1:** reviewed the reference `normalize.py` and fixed it, with regression tests.
  - **B2:** reviewed the reference `crypto.py` / `schema.sql` / `db.py`, fixed findings 1–7 (finding 8
    was deferred to B6), and wrote `config.py` + 11 tests.
  - **B3:** `ingest_leads.py` from my algorithm (18 tests).
  - **B4:** `ingest_events.py` (17 tests).
  - **B5:** `facts.py` as pure functions (45 tests).
  - **B6:** `reports.py` + CLI (`python -m solution ingest|report`). 193 tests in total.
  - **SUBMISSION.md:** first draft from the decision log, code and test results.
- **B7, `verify.py`:** the clean-room harness is the Claude (chat) reference version, which I had tested
  earlier against planted PII leaks.
- **SUBMISSION.md, final file:** Claude Code's file write was interrupted, so Claude (chat) rebuilt the
  clean file from that draft.

## What I wrote or decided myself
- **Architecture:** proposed in the Claude (chat) playbook: SQLite with plain SQL, separate ingest and
  report, an append-only ledger, linking at report time, file identity + SHA-256 idempotency, and
  AES-GCM + HMAC blind index. I reviewed it, adopted it and wrote it into `CLAUDE.md` as decided.

  My own call: I rejected the earlier "pending orphan + reconcile" approach because it mutates the
  ledger.
- **Manual data inspection (no AI):** I generated the seed-42 data and read the files in VS Code before
  any implementation. I saw:
  - `batch_003.csv` headers `Language,Insured Name,Policy No,Mobile No,Expiry Date,Premium,agent_notes`
    (aliases, a new column order, an extra column to ignore)
  - phones like `+916463745499`, `+91 84599 10229`, `09942921017`, `(+91) 9357136187`, `91-77187-50036`
  - amounts like `₹27,554`, `Rs. 26505`, `INR 18678.3`
  - languages like `" ta "`, `TA`, `Hindi`
  - dates like `21-11-2026`, `22-Sep-2026` and the invalid `2026-13-01`
- **Every spec ambiguity:** options were proposed by Claude Code (recon) or discussed with Claude (chat).
  I made the final call on each and logged it (D1–D51). Examples:
  - **D17:** a bad timestamp is skipped and counted separately; the file does not fail.
  - **D25:** a whitespace-only line is malformed.
  - **D28:** `end_reason` is added to the payload allowlist.
  - **D39:** a missing `event_type` is stored as `""`. Reason: `INSERT OR IGNORE` also silently skips
    NOT NULL violations, which would be miscounted as duplicates.
  - **D40:** an integer phone becomes text; any other non-string identity field becomes NULL, so the
    event is an orphan.
  - **D41:** each line is decoded separately, so one bad byte can't fail a file.
  - **D44:** an ambiguous call goes to the lead of the earliest `call.initiated`.
  - **D45:** a linked event without `call_id` is linked but is not a call.
  - **D46:** a missing disposition code counts as unknown.
  - **D47–D51:** exit codes, `report` refusing a missing DB, file discovery, the pre-check refusing an
    unknown workflow, and the rejection tie-break. Claude Code proposed these; I approved them.
- **The append-only fix:** I chose `PRAGMA recursive_triggers` over Claude Code's preferred fix (see
  below).
- **Accepted AI suggestions:**
  - B3: the identity phone field is found from the schema (`type == "phone"`) instead of being
    hard-coded.
  - B6: that helper moved into `config.py`, and every file is checked before ingesting any (D36).

## Where the AI was wrong or unhelpful
- **Reference `normalize.py` (Claude chat) had 3 real bugs,** found in the B1 review:
  - a decimal precision crash on long amounts
  - enums compared case-insensitively on one side only
  - Unicode digits accepted as phone digits

  All three are fixed with regression tests (D30).
- **Reference store code (Claude chat) had 8 issues** (B2 review), including a real append-only bypass:
  `INSERT OR REPLACE` deleted ledger rows without firing the DELETE trigger.
- **Claude Code's preferred fix for that bypass was wrong for this design.** It wanted a BEFORE INSERT
  trigger. Claude (chat) tested both options: the bypass itself, and that a BEFORE INSERT trigger also
  aborts `INSERT OR IGNORE`, which would break duplicate-event counting. I chose
  `PRAGMA recursive_triggers = ON` (D32). Regression tests in the repo cover both behaviours.
- **Recon recommendations I overrode:**
  - A16: I count a whitespace-only line as malformed (D25).
  - A8: I skip a bad timestamp with a separate count instead of failing the whole file (D17).
- **Suggestions I rejected:** in B1, changing the timestamp parser. It has no effect on graded output,
  so I documented it instead (D31).
- **Wrong interpreter (B3):** Claude Code's shell used the system Python, not the project venv. Fixed by
  adding an "always use `.venv/Scripts/python`" rule to `CLAUDE.md`.
- **Wrong test expectation (B5):** one test failed on its first run because the fixture's expected value
  was wrong (a tie on initiated time goes to the higher `call_id`, D13), not the code.
- **Workflow friction:**
  - B4: my permission rules blocked a long generated shell command, so Claude Code switched to a single
    script file.
  - B6: its end-to-end run was blocked, so I ran the pipeline myself.
  - SUBMISSION.md: its file write was interrupted (see above).
- **Decision log hygiene:** a broken table row (D38/D39) slipped in and was fixed in B5.

## How I verified AI-generated code
- **Unit tests per spec rule:** every implemented rule has a test citing it (193 passing at B6). I
  required real pytest output in each increment and never accepted a weakened or skipped test.
- **Independent cross-check numbers:** the Claude (chat) seed-42 numbers come from a separate reference
  implementation. Every smoke run matched them exactly:

  | Step | Seed-42 result |
  |---|---|
  | B3, lead ingest | 2057 rows / 108 rejected / 57 merged / 1892 leads; rejection reasons match |
  | B4, event ingest | 8745 lines / 8 malformed / 237 duplicates / 0 unusable timestamps |
  | B5, facts | orphans 1123 → 462 after `drop_2`; unknown dispositions 10 → 18; buckets 816/526/38/512 |
  | B6, full pipeline (my own run) | all ingest counts match |
- **Clean-room harness:** `python -m solution.verify --seeds 42 7 1234` reports ALL CHECKS PASSED. It
  checks:
  - re-ingest leaves reports byte-identical
  - incremental == all-at-once, byte-identical
  - 20 cross-file invariants
  - no plaintext phone or name in the DB or the reports
- **Reading and review:**
  - I read every plan before letting Claude Code edit and reviewed each diff before committing (git
    history: one commit per increment).
  - For the `INSERT OR REPLACE` fix, Claude (chat) tested both the bypass and the BEFORE INSERT
    side-effect, and the repo's regression tests (`solution/tests/test_db.py`) check both behaviours:
    `OR REPLACE` is blocked, and `OR IGNORE` still reports a duplicate.
- **Manual checks:**
  - I inspected the generated data myself before writing any parser rules.
  - I checked the name, time spent and run commands in SUBMISSION.md.
