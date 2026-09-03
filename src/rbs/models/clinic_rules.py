"""Clinic scheduling rules: slots, rules, allocation, and closure days."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic_site import (
    _WEEKDAY_OFFSETS,
    ALL_CLINIC_SITES,
    ClinicSiteConfig,
    normalize_clinic_site_ids,
)
from rbs.models.common import StrictModel
from rbs.models.enums import WEEKDAYS_MF, Session, Weekday


def clinic_slot_date(first_week_start: date, week: int, weekday: Weekday) -> date:
    """Return the calendar date for a week-specific clinic slot."""
    return first_week_start + timedelta(
        weeks=week - 1,
        days=_WEEKDAY_OFFSETS[weekday],
    )


class ClinicAllocationRule(StrictModel):
    """Resident-level allocation bounds and target for one clinic."""

    clinic_id: str
    pgy: int | None = Field(default=None, ge=1)
    resident_id: str | None = None
    min_fraction: float = Field(default=0.0, ge=0.0, le=1.0)
    target_fraction: float = Field(ge=0.0, le=1.0)
    max_fraction: float = Field(default=1.0, ge=0.0, le=1.0)

    @field_validator("clinic_id")
    @classmethod
    def normalize_clinic_reference(cls, value: str) -> str:
        normalized = normalize_clinic_site_ids([value])
        if normalized == [ALL_CLINIC_SITES]:
            raise ValueError("allocation rules must reference one configured clinic")
        return normalized[0]

    @field_validator("resident_id")
    @classmethod
    def normalize_resident_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("resident allocation override ID cannot be empty")
        return normalized

    @model_validator(mode="after")
    def fractions_are_ordered(self) -> ClinicAllocationRule:
        if self.pgy is not None and self.resident_id is not None:
            raise ValueError(
                "clinic allocation rule cannot target both a training level and resident"
            )
        if not self.min_fraction <= self.target_fraction <= self.max_fraction:
            raise ValueError(
                "clinic allocation fractions must satisfy minimum <= target <= maximum"
            )
        return self

    @property
    def scope_key(self) -> tuple[str, int | str | None]:
        if self.resident_id is not None:
            return "resident", self.resident_id
        if self.pgy is not None:
            return "pgy", self.pgy
        return "overall", None


ClinicSiteConfig.model_rebuild(
    _types_namespace={"ClinicAllocationRule": ClinicAllocationRule},
)


class ClinicSlot(StrictModel):
    """One allowed (or required) half-day clinic assignment."""

    weekday: Weekday | None = Field(
        default=None,
        description="None means any weekday in the rotation's configured weekly set.",
    )
    session: Session | None = Field(
        default=None,
        description="None means morning or afternoon is allowed.",
    )
    sites: list[str] = Field(default_factory=list)
    preferred: bool = Field(
        default=False,
        description=(
            "Soft preference for this allowed weekday/session. Other allowed slots remain "
            "valid fallbacks when the preferred slot conflicts with stronger rules."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_site_fields(cls, value: Any) -> Any:
        """Accept persisted single-site slots while emitting only site lists."""
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        had_site = "site" in migrated
        site = migrated.pop("site", None)
        if "sites" not in migrated and had_site and site is not None:
            migrated["sites"] = [site]
        return migrated

    @field_validator("sites")
    @classmethod
    def unique_sites(cls, sites: list[str]) -> list[str]:
        return normalize_clinic_site_ids(sites)


class ClinicRule(StrictModel):
    """Continuity-clinic constraints that apply while a resident is on this rotation."""

    half_days_per_week: int = Field(default=1, ge=0)
    slots: list[ClinicSlot] = Field(
        default_factory=list,
        description="Allowed clinic slots. Empty means no clinic.",
    )
    max_concurrent: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Maximum residents on this rotation who may attend clinic in the same "
            "half-day. None leaves the overall concurrency unconstrained."
        ),
    )
    max_concurrent_by_pgy: dict[int, int] = Field(
        default_factory=dict,
        description=(
            "Optional per-training-level maxima for residents on this rotation "
            "attending clinic in the same half-day."
        ),
    )
    admin_half_days_per_week: int = Field(
        default=0,
        ge=0,
        description="Number of enabled weekly sessions reserved for administrative time.",
    )
    no_academic_day_attendance: bool = Field(
        default=False,
        description="Resident does not attend the program's academic half day.",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_slots(cls, value: Any) -> Any:
        """Normalize historical slot and concurrency representations."""
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_unique = migrated.pop("unique_among_concurrent", None)
        if "max_concurrent" not in migrated and legacy_unique:
            migrated["max_concurrent"] = 1
        legacy_admin = migrated.pop("admin_half_day", None)
        if "admin_half_days_per_week" not in migrated and legacy_admin is not None:
            migrated["admin_half_days_per_week"] = 1 if legacy_admin else 0
        concrete: list[dict[str, Any] | ClinicSlot] = []
        saw_legacy_shape = False
        for item in migrated.get("slots") or []:
            if isinstance(item, ClinicSlot):
                raw = item.model_dump(mode="json")
            elif isinstance(item, dict):
                raw = dict(item)
            else:
                concrete.append(item)
                continue

            legacy = (
                "sites" not in raw
                or "site" in raw
                or not raw.get("weekday")
                or not raw.get("session")
            )
            saw_legacy_shape = saw_legacy_shape or legacy
            old_site = raw.pop("site", None)
            if "sites" not in raw:
                raw["sites"] = [old_site] if old_site is not None else [ALL_CLINIC_SITES]
            weekdays = [raw.get("weekday")] if raw.get("weekday") else list(WEEKDAYS_MF)
            sessions = [raw.get("session")] if raw.get("session") else list(Session)
            for weekday in weekdays:
                for session in sessions:
                    concrete.append(
                        {
                            "weekday": weekday,
                            "session": session,
                            "sites": list(raw.get("sites") or []),
                            "preferred": bool(raw.get("preferred")),
                        }
                    )
        if saw_legacy_shape:
            by_time: dict[tuple[Any, Any], dict[str, Any]] = {}
            for slot in concrete:
                if not isinstance(slot, dict):
                    continue
                key = (slot.get("weekday"), slot.get("session"))
                merged = by_time.setdefault(
                    key,
                    {
                        "weekday": slot.get("weekday"),
                        "session": slot.get("session"),
                        "sites": [],
                        "preferred": False,
                    },
                )
                merged["sites"] = list(
                    dict.fromkeys([*merged["sites"], *list(slot.get("sites") or [])])
                )
                merged["preferred"] = bool(
                    merged["preferred"] or slot.get("preferred")
                )
            concrete = list(by_time.values())
        migrated["slots"] = concrete
        return migrated

    @field_validator("max_concurrent_by_pgy")
    @classmethod
    def valid_pgy_concurrency_limits(cls, limits: dict[int, int]) -> dict[int, int]:
        for pgy, maximum in limits.items():
            if pgy < 1:
                raise ValueError("clinic concurrency training-level key must be positive")
            if maximum < 1:
                raise ValueError("clinic concurrency maximum must be at least 1")
        return dict(sorted(limits.items()))

    @model_validator(mode="after")
    def concrete_slots_have_sites(self) -> ClinicRule:
        seen: set[tuple[Weekday, Session]] = set()
        for slot in self.slots:
            if slot.weekday is None or slot.session is None:
                raise ValueError("clinic half-days must select a day and session")
            if not slot.sites:
                raise ValueError("each allowed clinic half-day must select at least one site")
            key = (slot.weekday, slot.session)
            if key in seen:
                raise ValueError("allowed clinic half-days cannot contain duplicate slots")
            seen.add(key)
        if self.half_days_per_week > len(seen):
            raise ValueError("half_days_per_week cannot exceed the allowed clinic half-days")
        if self.admin_half_days_per_week > len(seen):
            raise ValueError("admin_half_days_per_week cannot exceed the allowed clinic half-days")
        return self

    def expanded_slots(self) -> list[ClinicSlot]:
        """Return the concrete, unique weekday/session choices."""
        expanded: list[ClinicSlot] = []
        seen: set[tuple[Weekday, Session]] = set()
        for slot in self.slots:
            if slot.weekday is None or slot.session is None:
                continue
            key = (slot.weekday, slot.session)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(slot.model_copy(deep=True))
        return expanded

    @property
    def unique_among_concurrent(self) -> bool:
        """Compatibility view of the former yes/no concurrency setting."""
        return self.max_concurrent == 1

    def max_concurrent_for_pgy(self, pgy: int) -> int | None:
        """Return the configured clinic-session cap for one training level, if any."""
        return self.max_concurrent_by_pgy.get(pgy)

    def without_slot(self, weekday: Weekday, session: Session) -> list[ClinicSlot]:
        return [
            slot
            for slot in self.expanded_slots()
            if not (slot.weekday is weekday and slot.session is session)
        ]


class ClinicClosureDay(StrictModel):
    """A full calendar day when selected clinic sites cannot schedule residents."""

    date: date
    sites: list[str] = Field(min_length=1)
    name: str = Field(default="", max_length=120)

    @field_validator("sites")
    @classmethod
    def concrete_sites(cls, sites: list[str]) -> list[str]:
        normalized = normalize_clinic_site_ids(sites)
        if normalized == [ALL_CLINIC_SITES]:
            raise ValueError("closure days must select specific configured clinic sites")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()
