"""Portable, versioned full-database state for ``.rbsc`` files."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from rbs.models.catalog import ConstraintCatalog
from rbs.models.common import StrictModel
from rbs.models.instance import SchedulingCase
from rbs.models.rotation import DEFAULT_ROTATION_COLOR, default_rotation_color
from rbs.models.schedule import Schedule

RBSC_FORMAT = "rbsc"
# Schemas 9 through 11 migrate on import; older documents fail validation
# instead of being guessed forward. Portable documents omit
# application-owned presentation (colors, solver tuning, automatic-locking
# state) by design; import restores neutral defaults. A Save As deliberately
# clears the bundled-sample flag before producing the user's document.
RBSC_SCHEMA_VERSION = 12
_AUTOMATIC_LOCK_SOURCE = "through_today"
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def portable_case_payload(case: SchedulingCase | dict[str, Any]) -> dict[str, Any]:
    """Return the direct workspace data that belongs in an RBSC document."""
    payload = case.model_dump(mode="json") if isinstance(case, SchedulingCase) else deepcopy(case)
    payload.pop("color_scheme", None)
    payload.pop("solver", None)
    payload.pop("lock_through_today", None)
    payload["locks"] = [
        lock
        for lock in payload.get("locks", [])
        if not isinstance(lock, dict) or lock.get("source", "manual") != _AUTOMATIC_LOCK_SOURCE
    ]
    return payload


def portable_catalog_payload(
    catalog: ConstraintCatalog | dict[str, Any],
) -> dict[str, Any]:
    """Return catalog constraints without application-owned display colors."""
    payload = (
        catalog.model_dump(mode="json")
        if isinstance(catalog, ConstraintCatalog)
        else deepcopy(catalog)
    )
    electives = payload.get("electives")
    if isinstance(electives, dict):
        electives.pop("color", None)
    for rotation in payload.get("rotations", []):
        if isinstance(rotation, dict):
            rotation.pop("color", None)
    clinic_policy = payload.get("clinic_policy")
    if isinstance(clinic_policy, dict):
        for site in clinic_policy.get("sites", []):
            if isinstance(site, dict):
                site.pop("color", None)
    return payload


def _hydrate_portable_preferences(value: object) -> object:
    """Restore presentation defaults stripped from portable documents."""
    if not isinstance(value, dict):
        return value
    hydrated = deepcopy(value)
    for record in hydrated.get("catalogs", []):
        if not isinstance(record, dict) or not isinstance(record.get("catalog"), dict):
            continue
        catalog = record["catalog"]
        electives = catalog.get("electives")
        if isinstance(electives, dict):
            electives.setdefault("color", DEFAULT_ROTATION_COLOR)
        for rotation in catalog.get("rotations", []):
            if isinstance(rotation, dict):
                rotation.setdefault(
                    "color",
                    default_rotation_color(str(rotation.get("id") or "")),
                )
        clinic_policy = catalog.get("clinic_policy")
        if isinstance(clinic_policy, dict):
            for site in clinic_policy.get("sites", []):
                if isinstance(site, dict):
                    site.setdefault(
                        "color",
                        default_rotation_color(str(site.get("id") or "")),
                    )
    for record in hydrated.get("workspaces", []):
        if not isinstance(record, dict) or not isinstance(record.get("case"), dict):
            continue
        case = record["case"]
        case.setdefault("color_scheme", {})
        case.setdefault("solver", {})
        case.setdefault("lock_through_today", False)
    return hydrated


def _migrate_portable_state(value: object) -> object:
    """Upgrade path for documents written by older schema versions.

    Version 12 folds dated attending shifts into their academic-week schedules,
    changes recurring category targets from one count to a minimum/maximum
    range, adds attending clinic-day and preferred-schedule configuration, and
    allows descriptions on Special/Other work. Accepted attending schedules
    move out of roster configuration into their own case-level collection.
    Each week that receives a dated shift uses its resulting assignment count
    as its half-day override. Versions 9 and 10 predate attending
    configuration; they upgrade to an empty attending directory while nested
    catalogs migrate independently (missing resident capacity defaults to
    one). Older shapes remain unsupported.

    Only the document-specific steps live here. The models themselves lift
    nested week-by-week schedules to the case level and turn a single weekly
    count into a range, because SQLite working copies and unversioned JSON
    carry those shapes too; new fields take their model defaults.
    """
    if not isinstance(value, dict) or value.get("schema_version") not in (9, 10, 11):
        return value
    source_version = value.get("schema_version")
    migrated = deepcopy(value)
    migrated["schema_version"] = RBSC_SCHEMA_VERSION
    for workspace in migrated.get("workspaces", []):
        if not isinstance(workspace, dict):
            continue
        case = workspace.get("case")
        if not isinstance(case, dict):
            continue
        if source_version in (9, 10):
            case.setdefault("attendings", [])
            case.setdefault("attending_schedules", [])
            continue
        calendar = case.get("calendar")
        first_week_start = (
            calendar.get("first_week_start") if isinstance(calendar, dict) else None
        )
        for attending in case.get("attendings", []):
            if isinstance(attending, dict):
                _migrate_version_11_attending(attending, first_week_start)
    return migrated


def _migrate_version_11_attending(
    attending: dict[str, Any],
    first_week_start: object,
) -> None:
    """Drop Special/Other targets and fold dated shifts into nested weeks."""
    targets = attending.get("weekly_shift_targets", [])
    if isinstance(targets, list):
        attending["weekly_shift_targets"] = [
            target
            for target in targets
            if not (isinstance(target, dict) and target.get("work_type") == "special_other")
        ]
    dated_shifts = attending.pop("ad_hoc_work_half_days", [])
    if not isinstance(dated_shifts, list):
        raise ValueError("version 11 attending shifts must be a list")
    if not dated_shifts:
        return
    try:
        first_day = date.fromisoformat(str(first_week_start))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "version 11 attending shifts require a valid academic calendar"
        ) from exc
    schedules = attending.setdefault("weekly_work_schedules", [])
    if not isinstance(schedules, list):
        return
    schedules_by_week: dict[int, dict[str, Any]] = {
        schedule["week"]: schedule
        for schedule in schedules
        if isinstance(schedule, dict) and isinstance(schedule.get("week"), int)
    }
    touched_weeks: set[int] = set()
    for dated_shift in dated_shifts:
        if not isinstance(dated_shift, dict):
            raise ValueError("version 11 attending shifts must contain objects")
        try:
            calendar_day = date.fromisoformat(str(dated_shift.get("date")))
        except (TypeError, ValueError) as exc:
            raise ValueError("version 11 attending shifts require valid dates") from exc
        week = (calendar_day - first_day).days // 7 + 1
        session = dated_shift.get("session")
        schedule = schedules_by_week.get(week)
        if schedule is None:
            schedule = {"week": week, "half_days": []}
            schedules.append(schedule)
            schedules_by_week[week] = schedule
        half_days = schedule.setdefault("half_days", [])
        if not isinstance(half_days, list):
            continue
        assignment = {
            "work_type": dated_shift.get("work_type", "special_other"),
            "clinic_id": dated_shift.get("clinic_id"),
            "description": None,
            "weekday": _WEEKDAYS[calendar_day.weekday()],
            "session": session,
        }
        half_days[:] = [
            half_day
            for half_day in half_days
            if not (
                isinstance(half_day, dict)
                and half_day.get("weekday") == assignment["weekday"]
                and half_day.get("session") == session
            )
        ]
        half_days.append(assignment)
        touched_weeks.add(week)
    for week in touched_weeks:
        half_days = schedules_by_week[week].get("half_days")
        if isinstance(half_days, list):
            schedules_by_week[week]["half_days_override"] = len(half_days)


def _validated_timestamp(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("timestamp cannot be empty")
    try:
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO 8601") from exc
    return normalized


class RBSCCatalog(StrictModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    catalog: ConstraintCatalog
    created_at: str
    updated_at: str
    managed: bool = False

    _timestamps = field_validator("created_at", "updated_at")(_validated_timestamp)


class RBSCWorkspace(StrictModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    catalog_id: int = Field(ge=1)
    case: SchedulingCase
    schedule: Schedule | None = None
    instance_revision: int = Field(ge=1)
    workspace_revision: int = Field(default=1, ge=1)
    schedule_revision: int | None = Field(default=None, ge=1)
    created_at: str
    updated_at: str
    is_sample: bool = False
    exported_instance_revision: int | None = Field(default=None, ge=1)
    exported_schedule_revision: int | None = Field(default=None, ge=1)
    exported_workspace_revision: int | None = Field(default=None, ge=1)
    exported_at: str | None = None

    _timestamps = field_validator("created_at", "updated_at")(_validated_timestamp)

    @field_validator("exported_at")
    @classmethod
    def valid_export_timestamp(cls, value: str | None) -> str | None:
        return None if value is None else _validated_timestamp(value)

    @model_validator(mode="after")
    def consistent_workspace(self) -> RBSCWorkspace:
        if self.academic_year != self.case.academic_year:
            raise ValueError("workspace academic_year must match its scheduling case")
        if self.schedule is None and self.schedule_revision is not None:
            raise ValueError("schedule_revision requires a schedule")
        if self.schedule is not None and self.schedule_revision is None:
            raise ValueError("a stored schedule requires schedule_revision")
        if self.schedule_revision is not None and self.schedule_revision > self.instance_revision:
            raise ValueError("schedule_revision cannot exceed instance_revision")
        if (
            self.exported_instance_revision is not None
            and self.exported_instance_revision > self.instance_revision
        ):
            raise ValueError("exported_instance_revision cannot exceed instance_revision")
        if (
            self.exported_workspace_revision is not None
            and self.exported_workspace_revision > self.workspace_revision
        ):
            raise ValueError("exported_workspace_revision cannot exceed workspace_revision")
        if self.exported_at is None and self.exported_instance_revision is not None:
            raise ValueError("an export revision requires an export timestamp")
        if self.schedule is not None and self.schedule.meta.academic_year != self.academic_year:
            raise ValueError("schedule academic_year must match its workspace")
        return self


class RBSCState(StrictModel):
    """The complete portable state of one RBS SQLite database."""

    format: Literal["rbsc"] = RBSC_FORMAT
    schema_version: Literal[12] = RBSC_SCHEMA_VERSION
    exported_at: str
    current_workspace_id: int | None = Field(default=None, ge=1)
    app_metadata: dict[str, str] = Field(default_factory=dict)
    catalogs: list[RBSCCatalog] = Field(default_factory=list)
    workspaces: list[RBSCWorkspace] = Field(default_factory=list)

    _export_timestamp = field_validator("exported_at")(_validated_timestamp)

    @model_validator(mode="before")
    @classmethod
    def migrate_and_hydrate(cls, value: object) -> object:
        return _hydrate_portable_preferences(_migrate_portable_state(value))

    @model_serializer(mode="wrap")
    def serialize_portable_state(self, handler):
        payload = handler(self)
        if self.schema_version < 4:
            return payload
        catalog_ids: dict[int, int] = {}
        canonical_catalogs: dict[str, dict[str, Any]] = {}
        portable_catalogs: list[dict[str, Any]] = []
        for record in payload.get("catalogs", []):
            if isinstance(record, dict) and isinstance(record.get("catalog"), dict):
                portable = portable_catalog_payload(record["catalog"])
                fingerprint = json.dumps(
                    portable,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                    default=str,
                )
                canonical = canonical_catalogs.get(fingerprint)
                if canonical is not None:
                    catalog_ids[int(record["id"])] = int(canonical["id"])
                    canonical["managed"] = bool(canonical.get("managed")) and bool(
                        record.get("managed")
                    )
                    continue
                record["catalog"] = portable
                canonical_id = int(record["id"])
                canonical_catalogs[fingerprint] = record
                catalog_ids[canonical_id] = canonical_id
                portable_catalogs.append(record)
        payload["catalogs"] = portable_catalogs
        for record in payload.get("workspaces", []):
            if isinstance(record, dict) and isinstance(record.get("case"), dict):
                record["case"] = portable_case_payload(record["case"])
                record["catalog_id"] = catalog_ids.get(
                    int(record["catalog_id"]),
                    int(record["catalog_id"]),
                )
        return payload

    @model_validator(mode="after")
    def references_are_consistent(self) -> RBSCState:
        catalog_ids = [catalog.id for catalog in self.catalogs]
        if len(catalog_ids) != len(set(catalog_ids)):
            raise ValueError("catalog ids must be unique")
        workspace_ids = [workspace.id for workspace in self.workspaces]
        if len(workspace_ids) != len(set(workspace_ids)):
            raise ValueError("workspace ids must be unique")
        if self.current_workspace_id is not None and self.current_workspace_id not in set(
            workspace_ids
        ):
            raise ValueError("current_workspace_id must reference an exported workspace")

        catalogs = {catalog.id: catalog.catalog for catalog in self.catalogs}
        for workspace in self.workspaces:
            catalog = catalogs.get(workspace.catalog_id)
            if catalog is None:
                raise ValueError(
                    f"workspace {workspace.id} references missing catalog {workspace.catalog_id}"
                )
            try:
                catalog.apply(workspace.case)
            except ValueError as exc:
                raise ValueError(
                    f"workspace {workspace.id} cannot use catalog {workspace.catalog_id}: {exc}"
                ) from exc
        return self
