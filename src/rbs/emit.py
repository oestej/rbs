"""JSON serialization for schedules, instances, and workspace bundles."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule

PathLike = str | Path


def to_jsonable(model: BaseModel) -> dict:
    return model.model_dump(mode="json")


def dumps(model: BaseModel, *, indent: int = 2) -> str:
    return json.dumps(to_jsonable(model), indent=indent) + "\n"


def write_json(model: BaseModel, path: PathLike) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(dumps(model), encoding="utf-8")
    return destination


def write_schedule(schedule: Schedule, path: PathLike) -> Path:
    return write_json(schedule, path)


def write_instance(instance: SchedulerInput, path: PathLike) -> Path:
    return write_json(instance, path)


def dumps_bundle(instance: SchedulerInput, schedule: Schedule | None = None) -> str:
    payload: dict = {"instance": to_jsonable(instance)}
    if schedule is not None:
        payload["schedule"] = to_jsonable(schedule)
    return json.dumps(payload, indent=2) + "\n"
