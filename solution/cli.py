"""Command line:  python -m solution ingest PATH [PATH ...] [--db FILE]
                python -m solution report --out DIR [--db FILE]

Exit codes: 0 ok; 1 refused (changed file, wrong keys, schema mismatch, ...); 2 usage error
(bad arguments, missing path, missing database for report).
Output is counts only: never names, phones or raw rows."""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from solution import config, db, reports
from solution.crypto import Vault, load_or_create_keys
from solution.ingest_events import ingest_event_file
from solution.ingest_leads import ingest_lead_file

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- drop discovery
def _is_drop(path: Path) -> bool:
    return (path / "leads").is_dir() or (path / "events").is_dir()


def expand_drops(paths: list[str]) -> list[Path]:
    """D18: drops in CLI order; a folder that is not itself a drop but holds drop_* folders
    expands to them sorted by name."""
    drops: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_dir():
            raise UsageError(f"path not found or not a directory: {raw}")
        if _is_drop(p):
            drops.append(p)
            continue
        children = sorted((c for c in p.iterdir() if c.is_dir() and c.name.startswith("drop_")),
                          key=lambda c: c.name)
        if not children:
            raise UsageError(f"{raw} is not a drop (no leads/ or events/) and has no drop_* folders")
        drops.extend(children)
    return drops


def drop_files(drop: Path) -> list[tuple[str, Path]]:
    """Ingest order inside one drop: lead files (workflow folders sorted, files sorted), then
    event files (sorted). Spec: "files in filename order"."""
    files: list[tuple[str, Path]] = []
    leads_dir, events_dir = drop / "leads", drop / "events"
    if leads_dir.is_dir():
        for wf_dir in sorted((d for d in leads_dir.iterdir() if d.is_dir()), key=lambda d: d.name):
            files += [("leads", f) for f in sorted(wf_dir.glob("*.csv"), key=lambda f: f.name)]
    if events_dir.is_dir():
        files += [("events", f) for f in sorted(events_dir.glob("*.jsonl"), key=lambda f: f.name)]
    return files


def source_key(drop: Path, path: Path) -> str:
    """D7: same logical identity the ingest modules use."""
    return f"{drop.name}/{path.relative_to(drop).as_posix()}"


def precheck(con, schemas, plan: list[tuple[Path, str, Path]]) -> None:
    """D36: before writing anything, check EVERY file in every given drop. A changed file
    (same identity, different bytes, already in the DB or twice on this command line) or an
    unknown workflow folder refuses the whole command. Raises SystemExit."""
    seen: dict[str, str] = {}
    for drop, kind, path in plan:
        if kind == "leads" and path.parent.name not in schemas:
            raise SystemExit(f"Unknown workflow folder {path.parent.name!r} in {drop.name}: "
                             f"no schema in assignment-a-data/schemas (known: {sorted(schemas)}).")
        key = source_key(drop, path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if seen.setdefault(key, sha) != sha:
            raise SystemExit(f"{key} is given twice with different content. Refusing.")
        db.ingest_status(con, key, sha)        # changed bytes -> SystemExit


# ---------------------------------------------------------------- commands
def _summary(kind: str, stats: dict) -> str:
    if stats["skipped"]:
        return f"  {stats['source_key']}: already ingested, skipped"
    if kind == "leads":
        line = (f"  {stats['source_key']}: rows={stats['rows_total']} rejected={stats['rows_rejected']}"
                f" merged={stats['rows_merged']} field_warnings={stats['field_warnings']}")
        if stats["header_collisions"]:          # field names (schema text), never cell values
            line += f" header_collisions={','.join(stats['header_collisions'])}"
        return line
    return (f"  {stats['source_key']}: lines={stats['lines_total']} stored={stats['stored']}"
            f" malformed={stats['malformed']} duplicates={stats['duplicates']}"
            f" unusable_timestamp={stats['unusable_timestamp']}")


def cmd_ingest(args, vault: Vault) -> int:
    drops = expand_drops(args.paths)
    schemas = config.load_schemas()
    files_by_drop = [(drop, drop_files(drop)) for drop in drops]
    plan = [(drop, kind, path) for drop, files in files_by_drop for kind, path in files]
    con = db.connect(args.db, vault)
    try:
        precheck(con, schemas, plan)            # D36: nothing is written if this refuses
        for drop, files in files_by_drop:
            print(f"{drop.name}:")
            if not files:
                print("  no lead or event files")
            for kind, path in files:
                if kind == "leads":
                    stats = ingest_lead_file(con, vault, schemas, drop, path)
                else:
                    stats = ingest_event_file(con, vault, drop, path)
                print(_summary(kind, stats))
    finally:
        con.close()
    return EXIT_OK


def cmd_report(args, vault: Vault) -> int:
    if not Path(args.db).is_file():
        raise UsageError(f"database not found: {args.db} (run ingest first)")
    con = db.connect(args.db, vault)
    try:
        reports.write_reports(con, vault, Path(args.out))
    finally:
        con.close()
    print(f"reports written to {args.out}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m solution")
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="ingest one or more drops (in the order given)")
    ing.add_argument("paths", nargs="+", metavar="PATH")
    ing.add_argument("--db", default=None, help="database file (default: $AU_DB or au_ledger.db)")
    rep = sub.add_parser("report", help="write the five report files")
    rep.add_argument("--out", required=True, metavar="DIR")
    rep.add_argument("--db", default=None, help="database file (default: $AU_DB or au_ledger.db)")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:                     # argparse: usage error (2) or --help (0)
        return e.code if isinstance(e.code, int) else EXIT_USAGE
    if args.db is None:
        args.db = db.default_db_path()
    try:
        vault = Vault(*load_or_create_keys())
        return cmd_ingest(args, vault) if args.command == "ingest" else cmd_report(args, vault)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as e:                     # our refusals carry a message, never PII
        if isinstance(e.code, int) or e.code is None:
            return e.code or EXIT_OK
        print(f"refused: {e.code}", file=sys.stderr)
        return EXIT_REFUSED
