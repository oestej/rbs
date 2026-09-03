"""Clinic directory: sites, capacity, closures, colors, and site IDs."""

from __future__ import annotations

import re
from datetime import date
from functools import cached_property
from typing import TYPE_CHECKING, Any

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel
from rbs.models.enums import Session, Weekday

if TYPE_CHECKING:
    # Resolved explicitly via ``ClinicSiteConfig.model_rebuild`` in
    # ``rbs.models.clinic_rules`` once the rule models exist.
    from rbs.models.clinic_rules import ClinicAllocationRule

ALL_CLINIC_SITES = "*"
_CLINIC_SITE_ID = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

_WEEKDAY_OFFSETS = {
    Weekday.MONDAY: 0,
    Weekday.TUESDAY: 1,
    Weekday.WEDNESDAY: 2,
    Weekday.THURSDAY: 3,
    Weekday.FRIDAY: 4,
    Weekday.SATURDAY: 5,
    Weekday.SUNDAY: 6,
}


def lighten_hex_color(color: str, *, white_mix: float = 0.9) -> str:
    """Calculate a pale tint by mixing a configured color with white."""
    if not _HEX_COLOR.fullmatch(color):
        raise ValueError("clinic site color must use #RRGGBB format")
    if not 0.0 <= white_mix <= 1.0:
        raise ValueError("white_mix must be between 0 and 1")
    channels = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
    light = [round(channel * (1.0 - white_mix) + 255 * white_mix) for channel in channels]
    return "#" + "".join(f"{channel:02X}" for channel in light)


def normalize_clinic_site_ids(site_ids: list[str]) -> list[str]:
    """Normalize stored clinic site references."""
    normalized: list[str] = []
    for value in site_ids:
        site_id = str(value).strip().lower().replace("-", "_")
        if not site_id:
            raise ValueError("clinic site IDs cannot be empty")
        if site_id != ALL_CLINIC_SITES and not _CLINIC_SITE_ID.fullmatch(site_id):
            raise ValueError("clinic site IDs must use lowercase letters, numbers, and underscores")
        if site_id not in normalized:
            normalized.append(site_id)
    if ALL_CLINIC_SITES in normalized:
        return [ALL_CLINIC_SITES]
    return normalized


class ClinicSiteClosure(StrictModel):
    """One date on which a single clinic is closed."""

    date: date
    name: str = Field(default="", max_length=120)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return value.strip()


class ClinicHalfDayCapacity(StrictModel):
    """Recurring staffing and capacity for one clinic half-day."""

    weekday: Weekday
    session: Session
    attendings: int = Field(default=1, ge=1)
    min_residents: int = Field(default=0, ge=0)

    def max_residents(self, residents_per_attending: int) -> int:
        """Return the derived resident ceiling for this half-day."""
        return self.attendings * residents_per_attending


class ClinicCapacityOverride(StrictModel):
    """Staffing and capacity replacing one clinic's recurring date/session."""

    date: date
    session: Session
    attendings: int = Field(default=1, ge=0)
    min_residents: int = Field(default=0, ge=0)

    def max_residents(self, residents_per_attending: int) -> int:
        """Return the override's derived resident ceiling."""
        return self.attendings * residents_per_attending


class ClinicSiteConfig(StrictModel):
    """One independently staffed, colored, and closable clinic."""

    id: str
    name: str = Field(min_length=1)
    color: str
    residents_per_attending: int = Field(default=4, ge=1)
    half_days: list[ClinicHalfDayCapacity] = Field(default_factory=list)
    capacity_overrides: list[ClinicCapacityOverride] = Field(default_factory=list)
    closure_days: list[ClinicSiteClosure] = Field(default_factory=list)
    allocation_rules: list[ClinicAllocationRule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_code(cls, value: Any) -> Any:
        """Accept historical clinic data while dropping its retired display code."""
        if not isinstance(value, dict) or "code" not in value:
            return value
        migrated = dict(value)
        migrated.pop("code")
        return migrated

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        if not _CLINIC_SITE_ID.fullmatch(normalized):
            raise ValueError("clinic site ID must use lowercase letters, numbers, and underscores")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("clinic site name cannot be empty")
        return normalized

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _HEX_COLOR.fullmatch(normalized):
            raise ValueError("clinic site color must use #RRGGBB format")
        return normalized

    @field_validator("half_days")
    @classmethod
    def unique_half_days(
        cls,
        half_days: list[ClinicHalfDayCapacity],
    ) -> list[ClinicHalfDayCapacity]:
        slots = [(item.weekday, item.session) for item in half_days]
        if len(slots) != len(set(slots)):
            raise ValueError("clinic capacity half-days must be unique")
        return sorted(
            half_days,
            key=lambda item: (
                _WEEKDAY_OFFSETS[item.weekday],
                list(Session).index(item.session),
            ),
        )

    @field_validator("closure_days")
    @classmethod
    def unique_closure_days(
        cls,
        closure_days: list[ClinicSiteClosure],
    ) -> list[ClinicSiteClosure]:
        dates = [closure.date for closure in closure_days]
        if len(dates) != len(set(dates)):
            raise ValueError("clinic closure dates must be unique within a clinic")
        return sorted(closure_days, key=lambda closure: closure.date)

    @field_validator("capacity_overrides")
    @classmethod
    def unique_capacity_overrides(
        cls,
        overrides: list[ClinicCapacityOverride],
    ) -> list[ClinicCapacityOverride]:
        slots = [(override.date, override.session) for override in overrides]
        if len(slots) != len(set(slots)):
            raise ValueError("clinic capacity overrides must use unique dates and sessions")
        return sorted(
            overrides,
            key=lambda override: (
                override.date,
                list(Session).index(override.session),
            ),
        )

    @model_validator(mode="after")
    def minimums_fit_derived_capacity(self) -> ClinicSiteConfig:
        for half_day in self.half_days:
            maximum = half_day.max_residents(self.residents_per_attending)
            if half_day.min_residents > maximum:
                raise ValueError(
                    f"{self.name} {half_day.weekday.value} {half_day.session.value}: "
                    "minimum residents cannot exceed derived maximum capacity"
                )
        for override in self.capacity_overrides:
            maximum = override.max_residents(self.residents_per_attending)
            if override.min_residents > maximum:
                raise ValueError(
                    f"{self.name} {override.date} {override.session.value} override: "
                    "minimum residents cannot exceed derived maximum capacity"
                )
        scopes = [rule.scope_key for rule in self.allocation_rules]
        if len(scopes) != len(set(scopes)):
            raise ValueError(f"{self.name} allocation overrides must use unique scopes")
        if any(rule.clinic_id != self.id for rule in self.allocation_rules):
            raise ValueError("clinic-owned allocation rules must reference their clinic")
        return self

    @property
    def light_color(self) -> str:
        return lighten_hex_color(self.color)

    @cached_property
    def _half_day_by_slot(self) -> dict[tuple[Weekday, Session], ClinicHalfDayCapacity]:
        index: dict[tuple[Weekday, Session], ClinicHalfDayCapacity] = {}
        for item in self.half_days:
            index.setdefault((item.weekday, item.session), item)
        return index

    @cached_property
    def _override_by_date_session(
        self,
    ) -> dict[tuple[date, Session], ClinicCapacityOverride]:
        index: dict[tuple[date, Session], ClinicCapacityOverride] = {}
        for override in self.capacity_overrides:
            index.setdefault((override.date, override.session), override)
        return index

    @cached_property
    def _closure_dates(self) -> frozenset[date]:
        return frozenset(closure.date for closure in self.closure_days)

    def half_day(
        self,
        weekday: Weekday,
        session: Session,
    ) -> ClinicHalfDayCapacity | None:
        return self._half_day_by_slot.get((weekday, session))

    def max_capacity(self, weekday: Weekday, session: Session) -> int:
        half_day = self.half_day(weekday, session)
        if half_day is None:
            return 0
        return half_day.max_residents(self.residents_per_attending)

    def capacity_override(
        self,
        calendar_day: date,
        session: Session,
    ) -> ClinicCapacityOverride | None:
        return self._override_by_date_session.get((calendar_day, session))

    def max_capacity_on(self, calendar_day: date, session: Session) -> int:
        """Return effective capacity after closures and date-specific overrides."""
        if self.is_closed(calendar_day):
            return 0
        override = self.capacity_override(calendar_day, session)
        if override is not None:
            return override.max_residents(self.residents_per_attending)
        weekday = tuple(Weekday)[calendar_day.weekday()]
        return self.max_capacity(weekday, session)

    def min_capacity_on(self, calendar_day: date, session: Session) -> int:
        """Return the effective minimum after closures and overrides."""
        if self.is_closed(calendar_day):
            return 0
        override = self.capacity_override(calendar_day, session)
        if override is not None:
            return override.min_residents
        weekday = tuple(Weekday)[calendar_day.weekday()]
        half_day = self.half_day(weekday, session)
        return half_day.min_residents if half_day is not None else 0

    def is_closed(self, calendar_day: date) -> bool:
        return calendar_day in self._closure_dates
