from collections import Counter
from datetime import date, timedelta
from functools import cached_property
from typing import Any, Self

from pydantic import Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from rbs.models.calendar import Calendar
from rbs.models.case_blocks import (
    AcademicHalfDayOverride,
    ManualClinicBlock,
    ResidentRotationOverride,
)
from rbs.models.catalog import ConstraintCatalog, validate_catalog_integrity
from rbs.models.clinic import ClinicPolicy, ClinicSiteConfig
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
    "Calendar",
    "ManualClinicBlock",
    "ObjectiveWeights",
    "ResidentRotationOverride",
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

    color_scheme: ColorScheme = Field(default_factory=ColorScheme)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    lock_through_today: bool = False

    @classmethod
    def from_instance(cls, instance: "SchedulerInput") -> "SchedulingCase":
        return cls(
            academic_year=instance.academic_year,
            calendar=instance.calendar,
            residents=instance.residents,
            color_scheme=instance.color_scheme,
            academic_half_day_overrides=instance.academic_half_day_overrides,
            locks=instance.locks,
            manual_clinic_blocks=instance.manual_clinic_blocks,
            resident_rotation_overrides=instance.resident_rotation_overrides,
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


class SolverClinicPolicy(ClinicPolicy):
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
    clinic_lock_cutoff_date: date | None = Field(
        default=None,
        description=(
            "Resolved date through which prior clinic occurrences are protected; "
            "derived from workspace workflow state before crossing the solver boundary."
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
    def _curriculum_by_pgy(self) -> dict[int, PGYCurriculum]:
        return {item.pgy: item for item in self.requirements}

    def revised(self, **updates: Any) -> Self:
        """Return a fully revalidated copy with ``updates`` applied.

        Every edit round-trips through validation so an instance can never hold
        a combination of fields that ``check_integrity`` would reject.
        """
        draft = self.model_copy(update=updates)
        return type(self).model_validate(draft.model_dump(mode="json"))

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
        for clinic in self.clinic_policy.sites:
            for override in clinic.capacity_overrides:
                if not first_day <= override.date <= last_day:
                    raise ValueError(
                        f"{clinic.name} capacity override {override.date.isoformat()} is "
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
            if override.weekday is recurring_day:
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
                if (
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
        self._check_resident_replacement_inventory()
        return self

    def constraint_catalog(self) -> ConstraintCatalog:
        return ConstraintCatalog.from_instance(self)

    def academic_half_day_for_week(self, week: int) -> tuple[Weekday, Session]:
        """Return the effective academic half-day for one academic week."""
        if not 1 <= week <= self.calendar.weeks:
            raise ValueError(f"academic week must be between 1 and {self.calendar.weeks}")
        override = self._academic_override_by_week.get(week)
        if override is not None:
            return override.weekday, override.session
        academic = self.clinic_policy.academic
        assert academic.weekday is not None and academic.session is not None
        return academic.weekday, academic.session

    def is_academic_half_day(
        self,
        week: int,
        weekday: Weekday,
        session: Session,
    ) -> bool:
        """Whether a half-day is Academic for this specific week."""
        academic_weekday, academic_session = self.academic_half_day_for_week(week)
        return weekday is academic_weekday and session is academic_session

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

    def curriculum_for(self, pgy: int) -> PGYCurriculum:
        try:
            return self._curriculum_by_pgy[pgy]
        except KeyError:
            raise KeyError(pgy) from None

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

    rotations: list[Rotation]
    electives: ElectiveConfiguration
    clinic_policy: ClinicPolicy
    clinic_lock_cutoff_date: SkipJsonSchema[None] = Field(default=None, exclude=True)
    color_scheme: ColorScheme = Field(default_factory=ColorScheme)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    lock_through_today: bool = False

    def scheduling_case(self) -> SchedulingCase:
        """Project the workspace instance onto its separately persisted case."""
        return SchedulingCase.from_instance(self)
