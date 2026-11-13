"""Paths plus loading of the workflow schemas and the disposition catalogue.
Everything workflow-specific comes from assignment-a-data/schemas/; nothing is hard-coded here."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent          # independent of the working directory
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"
DISPOSITIONS_FILE = SCHEMAS_DIR / "dispositions.csv"

_BOOLS = {"true": True, "false": False}


def load_schemas(schemas_dir: Path = SCHEMAS_DIR) -> dict[str, dict[str, Any]]:
    """workflow_id -> schema dict, one per *.json file."""
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            schema = json.load(fh)
        wf = schema.get("workflow_id")
        if not isinstance(wf, str) or not wf:
            raise ValueError(f"{path.name}: missing workflow_id")
        if wf in schemas:
            raise ValueError(f"{path.name}: duplicate workflow_id {wf!r}")
        schemas[wf] = schema
    return schemas


def _strict_bool(value: str, where: str) -> bool:
    try:
        return _BOOLS[value]
    except KeyError:
        raise ValueError(f"{where}: expected 'true' or 'false', got {value!r}") from None


def load_dispositions(path: Path = DISPOSITIONS_FILE) -> dict[tuple[str, str], tuple[bool, bool]]:
    """(workflow_id, code) -> (is_terminal, is_success). Codes are case-sensitive (D21)."""
    catalogue: dict[tuple[str, str], tuple[bool, bool]] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for line_no, row in enumerate(csv.DictReader(fh), start=2):
            where = f"{path.name} line {line_no}"
            key = (row["workflow_id"], row["code"])
            if key in catalogue:
                raise ValueError(f"{where}: duplicate disposition {key}")
            catalogue[key] = (_strict_bool(row["is_terminal"], where),
                              _strict_bool(row["is_success"], where))
    return catalogue


def reference_field(schema: dict[str, Any]) -> str:
    """Name of the single field marked "reference": true (becomes external_ref)."""
    refs = [f["name"] for f in schema["fields"] if f.get("reference") is True]
    if len(refs) != 1:
        raise ValueError(f"{schema.get('workflow_id')}: expected exactly one reference field, got {refs}")
    return refs[0]


def pii_fields(schema: dict[str, Any]) -> list[str]:
    """Fields marked "pii": true, in schema order (stored only inside pii_enc)."""
    return [f["name"] for f in schema["fields"] if f.get("pii") is True]


def defaults(schema: dict[str, Any]) -> dict[str, Any]:
    """field -> default, for fields that declare one (applied after merging)."""
    return {f["name"]: f["default"] for f in schema["fields"] if "default" in f}
