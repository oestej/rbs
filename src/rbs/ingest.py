from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rbs.catalog import catalog_dict
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.solver.validation import validate_schedule_or_raise

PathLike = str | Path


def load_json(path: PathLike) -> dict[str, Any]:
    payload = Path(path).read_text(encoding="utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def merge_catalog(raw: dict[str, Any], catalog_path: PathLike | None = None) -> dict[str, Any]:
    """Fill missing rotations/requirements from an explicit catalog or the default."""
    needs_rotations = not raw.get("rotations")
    needs_requirements = not raw.get("requirements")
    needs_policy = not raw.get("clinic_policy")
    # Case-only payloads obtain the elective policy and rotation groups with
    # the rest of their catalog.
    needs_electives = needs_rotations and not raw.get("electives")
    needs_rotation_groups = needs_rotations and "rotation_groups" not in raw
    if (
        not needs_rotations
        and not needs_requirements
        and not needs_policy
        and not needs_electives
        and not needs_rotation_groups
    ):
        return raw

    if catalog_path is not None:
        catalog = load_json(catalog_path)
    else:
        catalog = catalog_dict()

    merged = dict(raw)
    if needs_rotations:
        if "rotations" not in catalog:
            raise ValueError("catalog is missing rotations")
        merged["rotations"] = catalog["rotations"]
    if needs_requirements:
        if "requirements" not in catalog:
            raise ValueError("catalog is missing requirements")
        merged["requirements"] = catalog["requirements"]
    if needs_rotation_groups:
        merged["rotation_groups"] = catalog.get("rotation_groups", [])
    if needs_electives:
        if "electives" not in catalog:
            raise ValueError("catalog is missing electives")
        merged["electives"] = catalog["electives"]
    if needs_policy and catalog.get("clinic_policy"):
        merged["clinic_policy"] = catalog["clinic_policy"]
    return merged


def load_instance(path: PathLike, catalog_path: PathLike | None = None) -> SchedulerInput:
    raw = merge_catalog(load_json(path), catalog_path=catalog_path)
    return SchedulerInput.model_validate(raw)


def loads_instance(payload: str, catalog_path: PathLike | None = None) -> SchedulerInput:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return SchedulerInput.model_validate(merge_catalog(data, catalog_path=catalog_path))


def parse_workspace_payload(
    data: dict[str, Any], catalog_path: PathLike | None = None
) -> tuple[SchedulerInput, Schedule | None]:
    """Load either a bare instance JSON or a ``{instance, schedule}`` bundle."""
    if "instance" in data and isinstance(data["instance"], dict):
        instance_payload = data["instance"]
        instance = SchedulerInput.model_validate(
            merge_catalog(instance_payload, catalog_path=catalog_path)
        )
        schedule = None
        if data.get("schedule") is not None:
            schedule = Schedule.model_validate(data["schedule"])
            validate_schedule_or_raise(instance, schedule)
        return instance, schedule
    if "residents" not in data:
        raise ValueError("JSON must be an instance (with residents) or a workspace bundle")
    instance = SchedulerInput.model_validate(merge_catalog(data, catalog_path=catalog_path))
    return instance, None
