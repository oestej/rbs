"""Pure helpers for workspace storage (no SQLite connection needed)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from rbs.models.catalog import ConstraintCatalog
from rbs.models.instance import SchedulingCase
from rbs.models.rbsc import RBSCCatalog, RBSCWorkspace
from rbs.models.schedule import Schedule


def schedule_has_rotation(payload: str | None, rotation_id: str) -> bool:
    if not payload:
        return False
    raw = json.loads(payload)
    return any(
        assignment.get("rotation_id") == rotation_id
        for assignment in raw.get("assignments", [])
        if isinstance(assignment, dict)
    )


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_object(payload: str) -> dict:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def catalog_hash(catalog: ConstraintCatalog) -> str:
    canonical = json.dumps(
        catalog.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


# Backwards-compatible private aliases (previously defined in ``rbs.store``).
_schedule_has_rotation = schedule_has_rotation
_now = now_iso
_load_object = load_object
_catalog_hash = catalog_hash


@dataclass
class CatalogRecord:
    id: int
    name: str
    catalog: ConstraintCatalog
    content_hash: str
    created_at: str
    updated_at: str
    managed: bool = False

class StoreInvalidatedError(RuntimeError):
    """Raised when a hosted dataset was retired while a caller still held it."""

def _row_to_rbsc_catalog(row: sqlite3.Row) -> RBSCCatalog:
    return RBSCCatalog(
        id=int(row["id"]),
        name=str(row["name"]),
        catalog=ConstraintCatalog.model_validate_json(row["catalog_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        managed=bool(row["managed"]) if "managed" in row.keys() else False,
    )


def _row_to_rbsc_workspace(
    row: sqlite3.Row,
    *,
    clear_sample: bool = False,
) -> RBSCWorkspace:
    keys = row.keys()

    def optional(name: str):
        return row[name] if name in keys and row[name] is not None else None

    exported_at = optional("exported_at")
    return RBSCWorkspace(
        id=int(row["id"]),
        name=str(row["name"]),
        academic_year=str(row["academic_year"]),
        catalog_id=int(row["catalog_id"]),
        case=SchedulingCase.model_validate_json(row["instance_json"]),
        schedule=(
            Schedule.model_validate_json(row["schedule_json"]) if row["schedule_json"] else None
        ),
        instance_revision=int(row["instance_revision"]),
        workspace_revision=(
            int(row["workspace_revision"])
            if "workspace_revision" in keys
            else int(row["instance_revision"])
        ),
        schedule_revision=(
            int(row["schedule_revision"]) if row["schedule_revision"] is not None else None
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        is_sample=(
            False
            if clear_sample
            else bool(optional("is_sample"))
        ),
        exported_instance_revision=(
            int(value) if (value := optional("exported_instance_revision")) is not None else None
        ),
        exported_schedule_revision=(
            int(value) if (value := optional("exported_schedule_revision")) is not None else None
        ),
        exported_workspace_revision=(
            int(value) if (value := optional("exported_workspace_revision")) is not None else None
        ),
        exported_at=str(exported_at) if exported_at is not None else None,
    )
