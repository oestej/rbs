from collections import Counter
from datetime import date, timedelta
from functools import cached_property
from typing import Any, Self

from pydantic import Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from rbs.models.attending import (
    Attending,
    AttendingClinicCoverage,
    AttendingSchedule,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
    EffectiveAttendingWeek,
    attending_clinic_coverage,
    effective_attending_week,
    normalize_attending_schedules,
    normalize_attendings,
    validate_attending_academic_year,
)
from rbs.models.calendar import Calendar
from rbs.models.case_blocks import (
    AcademicHalfDayOverride,
    ManualClinicBlock,
    ResidentRotationOverride,
    ResidentRotationWaiver,
)
from rbs.models.catalog import ConstraintCatalog, validate_catalog_integrity
from rbs.models.clinic import ClinicPolicy, ClinicSiteConfig, ClinicStaffingMode
from rbs.models.color_scheme import ColorScheme
from rbs.models.common import StrictModel
from rbs.models.curriculum import (
    PGYCurriculum,
    RotationGroup,
    default_training_level_code,
    default_training_level_name,
)
from rbs.models.elective import (
    ElectiveConfiguration,
    apply_elective_option_defaults,
    apply_shared_elective_color,
)
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.locks import LockedPlacement
from rbs.models.problem_checks import SolverIntegrityMixin
from rbs.models.problem_electives import ElectiveQueriesMixin
from rbs.models.resident import ElectivePreferenceRequest, Resident
from rbs.models.rotation import DEFAULT_ROTATION_COLOR, Rotation
from rbs.models.solver_options import ObjectiveWeights, SolverConfig
from rbs.models.special import SpecialRotation, SpecialRotationKind

__all__ = [
    "AcademicHalfDayOverride",
    "AttendingClinicCoverage",
    "Calendar",
    "ManualClinicBlock",
    "ObjectiveWeights",
    "ResidentRotationOverride",
    "ResidentRotationWaiver",
    "SchedulingCase",
    "SchedulerInput",
    "SolverCase",
    "SolverClinicPolicy",
    "SolverClinicSiteConfig",
    "SolverConfig",
    "SolverElectiveConfiguration",
    "SolverProblem",
    "SolverRotation",
]


def _migrate_nested_attending_schedules(value: object) -> object:
    """Lift the pre-v12 schedule shape without mutating the caller's payload.

    Local SQLite working copies and unversioned JSON inputs do not carry an
    independent format version. Accepting this one unambiguous prior shape
    protects schedules created before they moved out of attending setup.
    """
    if not isinstance(value, dict) or not isinstance(value.get("attendings"), list):
        return value

    migrated = dict(value)
    attendings: list[object] = []
    legacy_schedules: list[dict[str, object]] = []
    for attending in value["attendings"]:
        if not isinstance(attending, dict):
            attendings.append(attending)
            continue
        revised_attending = dict(attending)
        weeks = revised_attending.pop("weekly_work_schedules", None)
        attendings.append(revised_attending)
        if weeks:
            legacy_schedules.append(
                {
                    "attending_id": revised_attending.get("id"),
                    "weeks": weeks,
                }
            )

    if legacy_schedules and migrated.get("attending_schedules"):
        raise ValueError(
            "attending schedules cannot use both nested and case-level formats"
        )
    migrated["attendings"] = attendings
    if legacy_schedules:
        migrated["attending_schedules"] = legacy_schedules
    return migrated


class SolverCase(StrictModel):
    """Workspace-specific facts that affect the mathematical problem."""

    academic_year: str
    calendar: Calendar
    residents: list[Resident]
    academic_half_day_overrides: list[AcademicHalfDayOverride] = Field(
        default_factory=list,
        description=(
            "Week-specific academic half-days that replace the recurring clinic policy slot."
        ),
    )
    locks: list[LockedPlacement] = Field(default_factory=list)
    manual_clinic_blocks: list[ManualClinicBlock] = Field(default_factory=list)
    resident_rotation_overrides: list[ResidentRotationOverride] = Field(default_factory=list)
    resident_rotation_waivers: list[ResidentRotationWaiver] = Field(default_factory=list)
    special_rotations: list[SpecialRotation] = Field(
        default_factory=list,
        description=(
            "Dated Conference/Multi-Day rotations and Half/Single Day events assigned to residents."
        ),
    )

    @cached_property
    def residents_by_id(self) -> dict[str, Resident]:
        return {resident.id: resident for resident in self.residents}

    @cached_property
    def _academic_override_by_week(self) -> dict[int, AcademicHalfDayOverride]:
        return {override.week: override for override in self.academic_half_day_overrides}

    @cached_property
    def special_rotations_by_id(self) -> dict[str, SpecialRotation]:
        return {special.id: special for special in self.special_rotations}

    @field_validator("academic_year")
    @classmethod
    def year_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("academic_year cannot be empty")
        return value.strip()

    @field_validator("academic_half_day_overrides")
    @classmethod
    def unique_academic_override_weeks(
        cls,
        overrides: list[AcademicHalfDayOverride],
    ) -> list[AcademicHalfDayOverride]:
        weeks = [override.week for override in overrides]
        if len(weeks) != len(set(weeks)):
            raise ValueError("academic half-day overrides must use unique weeks")
        return sorted(overrides, key=lambda override: override.week)

    @field_validator("special_rotations")
    @classmethod
    def ordered_special_rotations(
        cls,
        rotations: list[SpecialRotation],
    ) -> list[SpecialRotation]:
        ids = [rotation.id for rotation in rotations]
        if len(ids) != len(set(ids)):
            raise ValueError("special rotation IDs must be unique")
        return sorted(
            rotations,
            key=lambda rotation: (
                rotation.start_date,
                rotation.end_date,
                rotation.name.casefold(),
                rotation.id,
            ),
        )


class SchedulingCase(SolverCase):
    """Persisted workspace case, including presentation and UI workflow state."""

    attendings: list[Attending] = Field(default_factory=list)
    attending_schedules: list[AttendingSchedule] = Field(default_factory=list)
    color_scheme: ColorScheme = Field(default_factory=ColorScheme)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    lock_through_today: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_nested_attending_schedules(cls, value: object) -> object:
        return _migrate_nested_attending_schedules(value)

    @field_validator("attendings")
    @classmethod
    def unique_attendings(cls, attendings: list[Attending]) -> list[Attending]:
        return normalize_attendings(attendings)

    @field_validator("attending_schedules")
    @classmethod
    def unique_attending_schedules(
        cls,
        schedules: list[AttendingSchedule],
    ) -> list[AttendingSchedule]:
        return normalize_attending_schedules(schedules)

    @model_validator(mode="after")
    def attending_dates_fit_calendar(self) -> Self:
        first_day = self.calendar.first_week_start
        last_day = first_day + timedelta(days=self.calendar.weeks * 7 - 1)
        validate_attending_academic_year(
            self.attendings,
            attending_schedules=self.attending_schedules,
            first_day=first_day,
            last_day=last_day,
        )
        return self

    @classmethod
    def from_instance(cls, instance: "SchedulerInput") -> "SchedulingCase":
        return cls(
            academic_year=instance.academic_year,
            calendar=instance.calendar,
            residents=instance.residents,
            attendings=instance.attendings,
            attending_schedules=instance.attending_schedules,
            color_scheme=instance.color_scheme,
            academic_half_day_overrides=instance.academic_half_day_overrides,
            locks=instance.locks,
            manual_clinic_blocks=instance.manual_clinic_blocks,
            resident_rotation_overrides=instance.resident_rotation_overrides,
            resident_rotation_waivers=instance.resident_rotation_waivers,
            special_rotations=instance.special_rotations,
            lock_through_today=instance.lock_through_today,
            solver=instance.solver,
        )


class SolverRotation(Rotation):
    """Rotation semantics without its block-schedule presentation color."""

    color: SkipJsonSchema[str] = Field(default=DEFAULT_ROTATION_COLOR, exclude=True)


class SolverClinicSiteConfig(ClinicSiteConfig):
    """Clinic scheduling facts without the calendar presentation color."""

    color: SkipJsonSchema[str] = Field(default="#000000", exclude=True)

    @model_validator(mode="after")
    def omit_dormant_capacity_configuration(self) -> Self:
        if self.staffing_mode is ClinicStaffingMode.ATTENDING_MANAGED:
            self.half_days = []
            self.capacity_overrides = []
        return self


class SolverClinicPolicy(ClinicPolicy):
    academic_half_day_is_attending_admin_time: SkipJsonSchema[bool] = Field(
        default=True,
        exclude=True,
    )
    sites: list[SolverClinicSiteConfig] = Field(min_length=1)
    notes: SkipJsonSchema[str] = Field(default="", exclude=True)


class SolverElectiveConfiguration(ElectiveConfiguration):
    color: SkipJsonSchema[str] = Field(default=DEFAULT_ROTATION_COLOR, exclude=True)


class SolverProblem(SolverIntegrityMixin, ElectiveQueriesMixin, SolverCase):
    """Self-contained, UI-independent input accepted by solver implementations."""

    rotations: list[SolverRotation]
    requirements: list[PGYCurriculum] = Field(min_length=1)
    rotation_groups: list[RotationGroup] = Field(default_factory=list)
    electives: SolverElectiveConfiguration
    clinic_policy: SolverClinicPolicy
    attending_coverage: list[AttendingClinicCoverage] = Field(
        default_factory=list,
        description=(
            "Derived dated preceptor counts for attending-managed clinics; "
            "full attending records remain outside the solver boundary."
        ),
    )
    clinic_lock_cutoff_date: date | None = Field(
        default=None,
        description=(
            "Resolved date through which prior clinic occurrences are protected; "
            "derived from workspace workflow state before crossing the solver boundary."
        ),
    )

    @field_validator("attending_coverage")
    @classmethod
    def ordered_attending_coverage(
        cls,
        coverage: list[AttendingClinicCoverage],
    ) -> list[AttendingClinicCoverage]:
        return sorted(
            coverage,
            key=lambda item: (
                item.date,
                list(Session).index(item.session),
                item.clinic_id,
            ),
        )

    @classmethod
    def from_instance(
        cls,
        instance: "SolverProblem",
        *,
        today: date | None = None,
    ) -> "SolverProblem":
        """Project a workspace instance onto the stable solver-facing fields."""
        payload = instance.model_dump(
            mode="json",
            include=set(cls.model_fields),
        )
        if hasattr(instance, "attending_schedules"):
            payload["attending_coverage"] = [
                item.model_dump(mode="json")
                for item in instance.attending_coverage
            ]
        if getattr(instance, "lock_through_today", False):
            payload["clinic_lock_cutoff_date"] = today or date.today()
        projected = cls.model_validate(payload)
        # Re-parse the wire form so excluded presentation fields cannot survive
        # merely because the source object happened to carry them in memory.
        return cls.model_validate_json(projected.model_dump_json())

    @cached_property
    def rotations_by_id(self) -> dict[str, Rotation]:
        return {rotation.id: rotation for rotation in self.rotations}

    @cached_property
    def _attending_coverage_by_slot(self) -> dict[tuple[str, date, Session], int]:
        return {
            (coverage.clinic_id, coverage.date, coverage.session): coverage.attendings
            for coverage in self.attending_coverage
        }

    def clinic_attending_count_on(
        self,
        site_id: str,
        calendar_day: date,
        session: Session,
    ) -> int:
        """Return effective scheduled preceptors for one managed clinic slot."""
        site = self.clinic_policy.site(site_id)
        if site.is_closed(calendar_day):
            return 0
        if site.staffing_mode is ClinicStaffingMode.CAPACITY_MANAGED:
            override = site.capacity_override(calendar_day, session)
            if override is not None:
                return override.attendings
            weekday = tuple(Weekday)[calendar_day.weekday()]
            half_day = site.half_day(weekday, session)
            return half_day.attendings if half_day is not None else 0
        return self._attending_coverage_by_slot.get(
            (site.id, calendar_day, session),
            0,
        )

    def clinic_max_capacity_on(
        self,
        site_id: str,
        calendar_day: date,
        session: Session,
    ) -> int:
        """Return resident capacity from the clinic's selected staffing source."""
        site = self.clinic_policy.site(site_id)
        if site.staffing_mode is ClinicStaffingMode.CAPACITY_MANAGED:
            return site.max_capacity_on(calendar_day, session)
        return self.clinic_attending_count_on(
            site.id,
            calendar_day,
            session,
        ) * site.residents_per_attending

    def clinic_min_capacity_on(
        self,
        site_id: str,
        calendar_day: date,
        session: Session,
    ) -> int:
        """Return the configured resident minimum for one clinic slot."""
        site = self.clinic_policy.site(site_id)
        if site.staffing_mode is ClinicStaffingMode.CAPACITY_MANAGED:
            return site.min_capacity_on(calendar_day, session)
        return 0

    @cached_property
    def _curriculum_by_pgy(self) -> dict[int, PGYCurriculum]:
        return {item.pgy: item for item in self.requirements}

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> Self:
        """Validate an edited JSON payload of this instance."""
        return cls.model_validate(raw)

    @model_validator(mode="after")
    def check_integrity(self) -> Self:
        self.electives = apply_elective_option_defaults(
            self.rotations,
            self.requirements,
            self.electives,
        )
        self.rotations = apply_shared_elective_color(self.rotations, self.electives)
        resident_ids = {resident.id for resident in self.residents}
        if len(resident_ids) != len(self.residents):
            raise ValueError("resident ids must be unique")
        unknown_allocation_residents = {
            rule.resident_id
            for rule in self.clinic_policy.allocation_rules
            if rule.resident_id is not None and rule.resident_id not in resident_ids
        }
        if unknown_allocation_residents:
            raise ValueError(
                "clinic allocation overrides reference unknown resident(s): "
                + ", ".join(sorted(unknown_allocation_residents))
            )

        first_day = self.calendar.first_week_start
        last_day = first_day + timedelta(days=self.calendar.weeks * 7 - 1)
        known_clinic_ids = set(self.clinic_policy.site_ids)
        coverage_slots: set[tuple[str, date, Session]] = set()
        for coverage in self.attending_coverage:
            if coverage.clinic_id not in known_clinic_ids:
                raise ValueError(
                    "attending coverage references unknown clinic "
                    f"{coverage.clinic_id!r}"
                )
            clinic = self.clinic_policy.site(coverage.clinic_id)
            if clinic.staffing_mode is not ClinicStaffingMode.ATTENDING_MANAGED:
                raise ValueError(
                    f"attending coverage references capacity-managed clinic {clinic.name!r}"
                )
            if not first_day <= coverage.date <= last_day:
                raise ValueError(
                    f"attending coverage {coverage.date.isoformat()} is outside academic "
                    f"year {first_day.isoformat()}..{last_day.isoformat()}"
                )
            slot = coverage.clinic_id, coverage.date, coverage.session
            if slot in coverage_slots:
                raise ValueError("attending coverage must use unique clinic date sessions")
            coverage_slots.add(slot)
        for clinic in self.clinic_policy.sites:
            for override in clinic.capacity_overrides:
                if not first_day <= override.date <= last_day:
                    raise ValueError(
                        f"{clinic.name} capacity override {override.date.isoformat()} is "
                        f"outside academic year {first_day.isoformat()}.."
                        f"{last_day.isoformat()}"
                    )
            for closure in clinic.closure_days:
                if not first_day <= closure.date <= last_day:
                    raise ValueError(
                        f"{clinic.name} closure day {closure.date.isoformat()} is "
                        f"outside academic year {first_day.isoformat()}.."
                        f"{last_day.isoformat()}"
                    )

        recurring_day = self.clinic_policy.academic.weekday
        for override in self.academic_half_day_overrides:
            if override.week > self.calendar.weeks:
                raise ValueError(
                    f"academic half-day override week {override.week} exceeds calendar of "
                    f"{self.calendar.weeks} weeks"
                )
            if override.cancels_week:
                continue
            if recurring_day is not None and override.weekday is recurring_day:
                raise ValueError(
                    f"academic half-day override week {override.week} must use a different "
                    "day from the recurring academic half-day"
                )

        validate_catalog_integrity(
            self.rotations,
            self.requirements,
            self.rotation_groups,
            self.calendar.weeks,
            self.clinic_policy,
            self.electives,
        )
        known = {rotation.id for rotation in self.rotations}
        curriculum_pgys = {item.pgy for item in self.requirements}
        missing_pgys = sorted({resident.pgy for resident in self.residents} - curriculum_pgys)
        if missing_pgys:
            labels = ", ".join(self.training_level_label(pgy) for pgy in missing_pgys)
            raise ValueError(f"residents have no curriculum for training level(s): {labels}")

        # Catalog and training-level edits can make previously saved requests
        # impossible. Keep the remaining stack in its original order and cap
        # duplicate requests at the direct inventory available for that shape.
        normalized_residents: list[Resident] = []
        for resident in self.residents:
            inventory = self.direct_elective_block_counts_for_pgy(resident.pgy)
            used: Counter[tuple[str, int]] = Counter()
            used_rotations: Counter[str] = Counter()
            preferences: list[ElectivePreferenceRequest] = []
            for request in resident.elective_preferences:
                option = self.electives.option_for(request.rotation_id)
                rotation = self.rotations_by_id.get(request.rotation_id)
                key = (request.rotation_id, request.duration_weeks)
                if (
                    option is None
                    or rotation is None
                    or request.duration_weeks not in inventory
                    or not option.allows(resident.pgy, request.duration_weeks)
                    or not rotation.allows_duration(
                        request.duration_weeks,
                        pgy=resident.pgy,
                    )
                    or used[key] >= inventory[request.duration_weeks]
                    or (not option.repeatable and used_rotations[request.rotation_id])
                ):
                    continue
                used[key] += 1
                used_rotations[request.rotation_id] += 1
                preferences.append(request)
            normalized_residents.append(
                resident
                if preferences == resident.elective_preferences
                else resident.model_copy(update={"elective_preferences": preferences})
            )
        self.residents = normalized_residents

        for resident in self.residents:
            for week in resident.vacation_weeks:
                if week > self.calendar.weeks:
                    raise ValueError(
                        f"{resident.id}: vacation week {week} exceeds calendar of "
                        f"{self.calendar.weeks} weeks"
                    )
            for day_off in resident.days_off:
                if not first_day <= day_off <= last_day:
                    raise ValueError(
                        f"{resident.id}: day off {day_off.isoformat()} is outside academic year "
                        f"{first_day.isoformat()}..{last_day.isoformat()}"
                    )
            for half_day in resident.clinic_half_days:
                resolved_sites = self.clinic_policy.resolve_site_ids(half_day.sites)
                unknown_sites = set(resolved_sites) - set(self.clinic_policy.site_ids)
                if unknown_sites:
                    raise ValueError(
                        f"{resident.id}: clinic half-day references unknown site(s): "
                        + ", ".join(sorted(unknown_sites))
                    )
                if self.clinic_policy.academic_enabled and (
                    half_day.weekday is self.clinic_policy.academic.weekday
                    and half_day.session is self.clinic_policy.academic.session
                ):
                    raise ValueError(
                        f"{resident.id}: clinic half-day cannot overlap the recurring "
                        "academic half-day"
                    )

        self._check_special_rotations(resident_ids, first_day, last_day)

        self._check_locks(known)
        self._check_manual_clinic_blocks(known)
        self._check_resident_rotation_overrides(known)
        self._check_resident_rotation_override_groups()
        self._check_resident_rotation_waivers(known)
        self._check_resident_replacement_inventory()
        return self

    def constraint_catalog(self) -> ConstraintCatalog:
        return ConstraintCatalog.from_instance(self)

    def academic_half_day_for_week(self, week: int) -> tuple[Weekday, Session] | None:
        """Return the effective academic half-day for one week, or None if it has none.

        A program can run no academic half-day at all, and any single week can
        cancel its own, so callers must treat the absence as ordinary.
        """
        if not 1 <= week <= self.calendar.weeks:
            raise ValueError(f"academic week must be between 1 and {self.calendar.weeks}")
        override = self._academic_override_by_week.get(week)
        if override is not None:
            # An override is that week's whole answer, including cancelling it.
            if override.cancels_week:
                return None
            return override.weekday, override.session
        academic = self.clinic_policy.academic
        if academic.weekday is None or academic.session is None:
            return None
        return academic.weekday, academic.session

    def is_academic_half_day(
        self,
        week: int,
        weekday: Weekday,
        session: Session,
    ) -> bool:
        """Whether a half-day is Academic for this specific week."""
        academic = self.academic_half_day_for_week(week)
        if academic is None:
            return False
        return weekday is academic[0] and session is academic[1]

    def has_academic_half_day_override(self, week: int) -> bool:
        return week in self._academic_override_by_week

    def special_rotations_for_resident(
        self,
        resident_id: str,
        *,
        calendar_day: date | None = None,
        session: Session | None = None,
        kind: SpecialRotationKind | None = None,
    ) -> tuple[SpecialRotation, ...]:
        """Return dated Special rotations matching a resident and optional slot."""
        return tuple(
            special
            for special in self.special_rotations
            if resident_id in special.resident_ids
            and (kind is None or special.kind is kind)
            and (calendar_day is None or special.blocks(calendar_day, session))
        )

    def resident_is_unavailable(
        self,
        resident_id: str,
        week: int,
        weekday: Weekday,
        session: Session | None = None,
    ) -> bool:
        """Whether vacation, time off, or a Special rotation blocks this slot."""
        resident = self.residents_by_id.get(resident_id)
        if resident is None:
            raise ValueError(f"unknown resident {resident_id!r}")
        if week in resident.vacation_weeks:
            return True
        return self.resident_clinic_is_blocked(
            resident_id,
            week,
            weekday,
            session,
        )

    def resident_clinic_is_blocked(
        self,
        resident_id: str,
        week: int,
        weekday: Weekday,
        session: Session | None = None,
    ) -> bool:
        """Whether dated time off or a Special rotation blocks a clinic slot."""
        resident = self.residents_by_id.get(resident_id)
        if resident is None:
            raise ValueError(f"unknown resident {resident_id!r}")
        calendar_day = self.calendar.first_week_start + timedelta(
            weeks=week - 1,
            days=list(Weekday).index(weekday),
        )
        if calendar_day in resident.days_off:
            return True
        return any(
            special.blocks(calendar_day, session)
            for special in self.special_rotations_for_resident(resident_id)
        )

    def resident_scheduling_vacation_weeks(self, resident_id: str) -> set[int]:
        """Vacation-rule weeks, including Conference/Multi-Day rotations.

        The block solver is weekly, so any week touched by a conference is
        vacation-like for block placement. Clinic suppression remains limited
        to the conference's exact dates.
        """
        resident = self.residents_by_id.get(resident_id)
        if resident is None:
            raise ValueError(f"unknown resident {resident_id!r}")
        weeks = set(resident.vacation_weeks)
        first_day = self.calendar.first_week_start
        for special in self.special_rotations_for_resident(
            resident_id,
            kind=SpecialRotationKind.CONFERENCE,
        ):
            for calendar_day in special.dates():
                weeks.add((calendar_day - first_day).days // 7 + 1)
        return weeks

    def rotation(self, rotation_id: str) -> Rotation:
        try:
            return self.rotations_by_id[rotation_id]
        except KeyError:
            raise KeyError(rotation_id) from None

    def rotation_group_for(self, pgy: int, rotation_id: str) -> RotationGroup | None:
        """Return the configured contiguous group containing this rotation, if any."""
        return next(
            (
                group
                for group in self.rotation_groups
                if group.pgy == pgy and rotation_id in group.rotation_ids
            ),
            None,
        )

    def anchored_rotation_group_for(
        self,
        pgy: int,
        rotation_id: str,
    ) -> RotationGroup | None:
        """Return the directional group anchored by this rotation, if any."""
        return next(
            (
                group
                for group in self.rotation_groups
                if group.pgy == pgy and group.anchor_rotation_id == rotation_id
            ),
            None,
        )

    def rotation_group_requiring(
        self,
        pgy: int,
        rotation_id: str,
    ) -> RotationGroup | None:
        """Return a group which requires this rotation to stay with its peers."""
        return next(
            (
                group
                for group in self.rotation_groups
                if group.pgy == pgy
                and (
                    group.anchor_rotation_id == rotation_id
                    or (
                        group.anchor_rotation_id is None
                        and rotation_id in group.rotation_ids
                    )
                )
            ),
            None,
        )

    def curriculum_for(self, pgy: int) -> PGYCurriculum:
        try:
            return self._curriculum_by_pgy[pgy]
        except KeyError:
            raise KeyError(pgy) from None

    def unallocated_weeks(self, pgy: int) -> int:
        """Calendar weeks this training level has not yet committed to a block.

        Curricula are built up rather than swapped: a level starts with every
        week unallocated and spends them on Mandatory, Clinic, and Elective
        requirements. Solving requires this to reach zero, but editing does not.
        """
        return self.calendar.weeks - self.curriculum_for(pgy).required_weeks()

    def resident_unallocated_weeks(self, resident_id: str) -> int:
        """Unscheduled weeks left after this resident's waivers and additions."""
        resident = self.residents_by_id.get(resident_id)
        if resident is None:
            raise KeyError(resident_id)
        released = sum(
            waiver.duration_weeks
            for waiver in self.resident_rotation_waivers
            if waiver.resident_id == resident_id
        )
        used = sum(
            override.duration_weeks
            for override in self.resident_rotation_overrides
            if override.resident_id == resident_id and override.replaces_rotation_id is None
        )
        used += sum(
            block.duration_weeks
            for block in self.manual_clinic_blocks
            if block.resident_id == resident_id and block.replaces_rotation_id is None
        )
        return self.unallocated_weeks(resident.pgy) + released - used

    @property
    def training_level_ids(self) -> tuple[int, ...]:
        """Configured training-level keys in their user-defined display order."""
        return tuple(curriculum.pgy for curriculum in self.requirements)

    def training_level_label(self, pgy: int, *, compact: bool = False) -> str:
        """Resolve the user-facing label for a stable training-level key."""
        try:
            curriculum = self.curriculum_for(pgy)
        except KeyError:
            return default_training_level_code(pgy) if compact else default_training_level_name(pgy)
        return curriculum.compact_label if compact else curriculum.display_label

    def training_level_code(self, pgy: int) -> str:
        """Resolve the configured code used by compact schedules and directories."""
        return self.training_level_label(pgy, compact=True)

    def training_level_name(self, pgy: int) -> str:
        """Resolve the configured full name used by headings and explanatory copy."""
        return self.training_level_label(pgy)

    @property
    def training_level_options(self) -> dict[int, str]:
        return {curriculum.pgy: curriculum.short_code for curriculum in self.requirements}

    @property
    def training_level_name_options(self) -> dict[int, str]:
        return {
            curriculum.pgy: (
                curriculum.short_code
                if curriculum.display_label == curriculum.short_code
                else f"{curriculum.short_code} — {curriculum.display_label}"
            )
            for curriculum in self.requirements
        }

    def training_level_sort_key(self, pgy: int) -> int:
        try:
            return self.training_level_ids.index(pgy)
        except ValueError:
            return len(self.training_level_ids)

    def residents_by_pgy(self) -> dict[int, list[Resident]]:
        grouped: dict[int, list[Resident]] = {pgy: [] for pgy in self.training_level_ids}
        for resident in self.residents:
            grouped.setdefault(resident.pgy, []).append(resident)
        return grouped

    def cohort_counts(self) -> dict[int, int]:
        return {pgy: len(members) for pgy, members in self.residents_by_pgy().items()}

    def rotation_ids_for_pgy(self, pgy: int) -> set[str]:
        curriculum = self.curriculum_for(pgy)
        ids = {block.rotation_id for block in curriculum.blocks}
        elective_durations = self.elective_block_durations_for_pgy(pgy)
        ids.update(
            option.rotation_id
            for option in self.electives.rotation_options
            if any(
                option.allows(pgy, duration)
                and self.rotation(option.rotation_id).allows_duration(duration, pgy=pgy)
                for duration in elective_durations
            )
        )
        ids.update(
            rotation.id
            for duration in self.direct_elective_block_counts_for_pgy(pgy)
            if (rotation := self.elective_fallback_rotation(pgy, duration)) is not None
        )
        return ids

    def available_weeks(
        self,
        pgy: int,
        rotation_id: str,
        *,
        elective: bool = False,
    ) -> int:
        curriculum = self.curriculum_for(pgy)
        if elective:
            option = self.electives.option_for(rotation_id)
            if option is None and self.is_elective_fallback_rotation(rotation_id, pgy):
                rotation = self.rotation(rotation_id)
                return sum(
                    duration * count
                    for duration, count in self.direct_elective_block_counts_for_pgy(pgy).items()
                    if rotation.allows_duration(duration, pgy=pgy)
                )
            if option is None:
                return 0
            rotation = self.rotation(rotation_id)
            total = sum(
                block.duration_weeks * block.count
                for block in curriculum.blocks
                if self.rotation(block.rotation_id).kind is RotationKind.ELECTIVE
                and option.allows(pgy, block.duration_weeks)
                and rotation.allows_duration(block.duration_weeks, pgy=pgy)
            )
            return total
        return sum(
            block.duration_weeks * block.count
            for block in curriculum.blocks
            if block.rotation_id == rotation_id
        )

    def elective_block_durations_for_pgy(self, pgy: int) -> set[int]:
        curriculum = self.curriculum_for(pgy)
        durations = {
            block.duration_weeks
            for block in curriculum.blocks
            if self.rotation(block.rotation_id).kind is RotationKind.ELECTIVE
        }
        return durations

    def block_durations_for_pgy(
        self,
        pgy: int,
        rotation_id: str,
        *,
        elective: bool = False,
    ) -> set[int]:
        curriculum = self.curriculum_for(pgy)
        if elective:
            option = self.electives.option_for(rotation_id)
            if option is None and self.is_elective_fallback_rotation(rotation_id, pgy):
                rotation = self.rotation(rotation_id)
                return {
                    duration
                    for duration in self.direct_elective_block_counts_for_pgy(pgy)
                    if rotation.allows_duration(duration, pgy=pgy)
                }
            if option is None:
                return set()
            rotation = self.rotation(rotation_id)
            return {
                duration
                for duration in self.elective_block_durations_for_pgy(pgy)
                if option.allows(pgy, duration) and rotation.allows_duration(duration, pgy=pgy)
            }
        return {
            block.duration_weeks for block in curriculum.blocks if block.rotation_id == rotation_id
        }

    @cached_property
    def _locked_assignment_by_resident_week(
        self,
    ) -> dict[tuple[str, int], tuple[str, bool]]:
        return {
            (lock.resident_id, week): (lock.rotation_id, lock.elective)
            for lock in self.locks
            for week in lock.weeks
        }

    def locked_rotation(self, resident_id: str, week: int) -> str | None:
        locked = self._locked_assignment_by_resident_week.get((resident_id, week))
        return locked[0] if locked is not None else None

    def locked_assignment(
        self,
        resident_id: str,
        week: int,
    ) -> tuple[str, bool] | None:
        return self._locked_assignment_by_resident_week.get((resident_id, week))


class SchedulerInput(SolverProblem):
    """Workspace instance: solver problem plus presentation/workflow settings."""

    attendings: list[Attending] = Field(default_factory=list)
    attending_schedules: list[AttendingSchedule] = Field(default_factory=list)
    attending_coverage: SkipJsonSchema[list[AttendingClinicCoverage]] = Field(
        default_factory=list,
        exclude=True,
    )
    rotations: list[Rotation]
    electives: ElectiveConfiguration
    clinic_policy: ClinicPolicy
    clinic_lock_cutoff_date: SkipJsonSchema[None] = Field(default=None, exclude=True)
    color_scheme: ColorScheme = Field(default_factory=ColorScheme)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    lock_through_today: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_nested_attending_schedules(cls, value: object) -> object:
        return _migrate_nested_attending_schedules(value)

    @field_validator("attendings")
    @classmethod
    def unique_attendings(cls, attendings: list[Attending]) -> list[Attending]:
        return normalize_attendings(attendings)

    @field_validator("attending_schedules")
    @classmethod
    def unique_attending_schedules(
        cls,
        schedules: list[AttendingSchedule],
    ) -> list[AttendingSchedule]:
        return normalize_attending_schedules(schedules)

    @model_validator(mode="after")
    def attending_dates_fit_calendar(self) -> Self:
        first_day = self.calendar.first_week_start
        last_day = first_day + timedelta(days=self.calendar.weeks * 7 - 1)
        validate_attending_academic_year(
            self.attendings,
            attending_schedules=self.attending_schedules,
            first_day=first_day,
            last_day=last_day,
        )
        known_clinic_ids = set(self.clinic_policy.site_ids)
        for attending in self.attendings:
            assignments = [
                *attending.preferred_weekly_schedule_half_days,
                *attending.schedule_template_half_days,
                *(
                    half_day
                    for schedule in self.attending_schedule_weeks(attending.id)
                    for half_day in schedule.half_days
                ),
            ]
            for assignment in assignments:
                if (
                    assignment.work_type is AttendingWorkType.PRECEPTING_CLINIC
                    and assignment.clinic_id not in known_clinic_ids
                ):
                    raise ValueError(
                        f"{attending.id}: Precepting Clinic work references unknown clinic "
                        f"{assignment.clinic_id!r}"
                    )
        self.attending_coverage = attending_clinic_coverage(
            self.effective_attending_weeks(),
            managed_clinic_ids={
                site.id
                for site in self.clinic_policy.sites
                if site.staffing_mode is ClinicStaffingMode.ATTENDING_MANAGED
            },
            closed_dates_by_clinic={
                site.id: {closure.date for closure in site.closure_days}
                for site in self.clinic_policy.sites
                if site.staffing_mode is ClinicStaffingMode.ATTENDING_MANAGED
            },
        )
        self.__dict__.pop("_attending_coverage_by_slot", None)
        return self

    def attending_schedule_for(self, attending_id: str) -> AttendingSchedule | None:
        """Return the accepted schedule for one attending, if it has any weeks."""
        return next(
            (
                schedule
                for schedule in self.attending_schedules
                if schedule.attending_id == attending_id
            ),
            None,
        )

    def attending_schedule_weeks(
        self,
        attending_id: str,
    ) -> tuple[AttendingWeeklyWorkSchedule, ...]:
        """Return one attending's accepted weeks without exposing a mutable default."""
        schedule = self.attending_schedule_for(attending_id)
        return tuple(schedule.weeks) if schedule is not None else ()

    def effective_attending_week(
        self,
        attending: Attending,
        week: int,
    ) -> EffectiveAttendingWeek:
        """Return one authoritative week after dates, vacation, and Academic."""
        first_day = self.calendar.first_week_start
        last_day = first_day + timedelta(days=self.calendar.weeks * 7 - 1)
        return effective_attending_week(
            attending,
            self.attending_schedule_for(attending.id),
            week=week,
            first_day=first_day,
            last_day=last_day,
            academic_half_day=self.academic_half_day_for_week(week),
            automatic_academic_admin=(
                self.clinic_policy.academic_half_day_is_attending_admin_time
            ),
        )

    def effective_attending_weeks(self) -> tuple[EffectiveAttendingWeek, ...]:
        """Return every attending week used by reports and coverage projection."""
        return tuple(
            self.effective_attending_week(attending, week)
            for attending in self.attendings
            for week in range(1, self.calendar.weeks + 1)
        )

    def attending_work_half_day_on(
        self,
        attending: Attending,
        calendar_day: date,
        session: Session,
    ) -> AttendingWorkHalfDay | None:
        """Resolve saved work plus the program's automatic academic Admin Time."""
        first_day = self.calendar.first_week_start
        last_day = first_day + timedelta(days=self.calendar.weeks * 7 - 1)
        if not first_day <= calendar_day <= last_day:
            return None
        week = (calendar_day - first_day).days // 7 + 1
        return self.effective_attending_week(attending, week).assignment_on(
            tuple(Weekday)[calendar_day.weekday()],
            session,
        )

    def attending_clinic_days_for_week(
        self,
        attending: Attending,
        week: int,
    ) -> frozenset[Weekday]:
        """Return distinct effective Attending Clinic weekdays in one week."""
        if not 1 <= week <= self.calendar.weeks:
            raise ValueError(
                f"academic week must be between 1 and {self.calendar.weeks}"
            )
        return self.effective_attending_week(attending, week).attending_clinic_days

    def attending_clinic_day_minimum_for_week(
        self,
        attending: Attending,
        week: int,
    ) -> int:
        """Return the minimum, excluding partial, inactive, and vacation weeks."""
        if not 1 <= week <= self.calendar.weeks:
            raise ValueError(
                f"academic week must be between 1 and {self.calendar.weeks}"
            )
        return self.effective_attending_week(
            attending,
            week,
        ).attending_clinic_day_minimum

    def scheduling_case(self) -> SchedulingCase:
        """Project the workspace instance onto its separately persisted case."""
        return SchedulingCase.from_instance(self)
