from datetime import UTC, datetime

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic import normalize_clinic_site_ids
from rbs.models.common import StrictModel
from rbs.models.enums import (
    RotationKind,
    Session,
    SolverEngineName,
    SolverStatus,
    Weekday,
)


class AssignedClinic(StrictModel):
    weekday: Weekday
    session: Session
    site: str | None = None
    allowed_sites: list[str] = Field(
        default_factory=list,
        description="Sites allowed by the source rotation half-day; empty for legacy output.",
    )
    admin: bool = False
    locked: bool = Field(
        default=False,
        description="Prevents this clinic occurrence from being moved in the schedule editor.",
    )
    automatic_lock_exempt: bool = Field(
        default=False,
        description=(
            "Records an explicit unlock while automatic through-today locking is enabled."
        ),
    )
    manual_override: bool = Field(
        default=False,
        description=(
            "Marks a clinic occurrence that was deliberately placed outside the generated "
            "schedule or its ordinary assignment rules."
        ),
    )
    manual_override_added: bool = Field(
        default=False,
        description="Marks an occurrence added beyond the generated weekly clinic load.",
    )
    manual_override_original_site: str | None = Field(
        default=None,
        description="Original site retained while a manual site reassignment is active.",
    )
    week: int | None = Field(
        default=None,
        description="When set, this slot applies to that week only.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_site_fields(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if normalized.get("site") is not None:
            normalized["site"] = normalize_clinic_site_ids([normalized["site"]])[0]
        if normalized.get("manual_override_original_site") is not None:
            normalized["manual_override_original_site"] = normalize_clinic_site_ids(
                [normalized["manual_override_original_site"]]
            )[0]
        if "allowed_sites" in normalized:
            normalized["allowed_sites"] = normalize_clinic_site_ids(
                list(normalized.get("allowed_sites") or [])
            )
        return normalized


class Assignment(StrictModel):
    resident_id: str
    rotation_id: str
    kind: RotationKind = RotationKind.STANDARD
    elective: bool = Field(
        default=False,
        description=(
            "The service fills Elective curriculum time. Mandatory services retain their "
            "normal kind while this marker supplies Elective display semantics."
        ),
    )
    elective_fallback: bool = Field(
        default=False,
        description=(
            "The assignment is Clinic filling otherwise unmatched direct Elective time."
        ),
    )
    start_week: int = Field(ge=1)
    end_week: int = Field(ge=1)
    weeks: list[int] = Field(min_length=1)
    block_start_week: int | None = Field(
        default=None,
        ge=1,
        description="Start of the source block configuration.",
    )
    block_duration_weeks: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="Duration of the source training-level block configuration.",
    )
    clinic_slots: list[AssignedClinic] = Field(default_factory=list)
    manual_clinic_baselines: dict[int, int] = Field(
        default_factory=dict,
        description=(
            "Original weekly clinic-slot counts captured before manual additions or removals."
        ),
    )
    vacation_weeks_during_block: list[int] = Field(default_factory=list)
    locked_weeks: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_range(self) -> "Assignment":
        expected = list(range(self.start_week, self.end_week + 1))
        if self.end_week < self.start_week or self.weeks != expected:
            raise ValueError(
                "assignment weeks must be the contiguous range from start_week through end_week"
            )
        if (self.block_start_week is None) != (self.block_duration_weeks is None):
            raise ValueError("block_start_week and block_duration_weeks must be set together")
        if self.block_start_week is not None and self.block_duration_weeks is not None:
            block_end = self.block_start_week + self.block_duration_weeks - 1
            if self.start_week < self.block_start_week or self.end_week > block_end:
                raise ValueError("assignment range must fit inside its source block")
        week_set = set(self.weeks)
        if not set(self.vacation_weeks_during_block) <= week_set:
            raise ValueError("vacation_weeks_during_block must be within assignment weeks")
        if not set(self.locked_weeks) <= week_set:
            raise ValueError("locked_weeks must be within assignment weeks")
        if not set(self.manual_clinic_baselines) <= week_set:
            raise ValueError("manual_clinic_baselines must be within assignment weeks")
        if any(count < 0 for count in self.manual_clinic_baselines.values()):
            raise ValueError("manual_clinic_baselines cannot contain negative counts")
        for slot in self.clinic_slots:
            if slot.week is not None and slot.week not in week_set:
                raise ValueError("week-specific clinic slots must be within assignment weeks")
        if self.elective_fallback and (
            not self.elective or self.kind is not RotationKind.CLINIC
        ):
            raise ValueError(
                "elective_fallback requires an Elective-marked Clinic assignment"
            )
        return self


class WeekCoverage(StrictModel):
    week: int = Field(ge=1)
    rotation_id: str
    elective: bool = False
    resident_ids: list[str]


class ScheduleMetrics(StrictModel):
    elective_fallback_blocks: int = Field(default=0, ge=0)
    elective_preference_rank_counts: list[int] = Field(
        default_factory=list,
        description="Matched request counts by one-based preference rank.",
    )
    primary_site_attending_sessions: int | None = Field(default=None, ge=0)
    primary_site_weekly_min: int | None = Field(default=None, ge=0)
    primary_site_weekly_max: int | None = Field(default=None, ge=0)
    primary_site_weekly_spread: int | None = Field(default=None, ge=0)
    clinic_block_weekly_min: int | None = Field(default=None, ge=0)
    clinic_block_weekly_max: int | None = Field(default=None, ge=0)
    clinic_block_weekly_spread: int | None = Field(default=None, ge=0)
    clinic_weekly_session_min: int | None = Field(default=None, ge=0)
    clinic_weekly_session_max: int | None = Field(default=None, ge=0)
    clinic_weekly_session_spread: int | None = Field(default=None, ge=0)
    allocation_target_sessions: int | None = Field(default=None, ge=0)
    allocation_assigned_sessions: int | None = Field(default=None, ge=0)
    allocation_target_shortfall: int | None = Field(default=None, ge=0)

    @field_validator("elective_preference_rank_counts")
    @classmethod
    def nonnegative_elective_rank_counts(cls, values: list[int]) -> list[int]:
        if any(value < 0 for value in values):
            raise ValueError("elective preference rank counts cannot be negative")
        return values

class SolverDiagnostic(StrictModel):
    """A structured, actionable explanation produced by the solver."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    resident_ids: list[str] = Field(default_factory=list)
    special_rotation_ids: list[str] = Field(default_factory=list)
    weeks: list[int] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class ScheduleMeta(StrictModel):
    """Final schedule outcome plus the raw solver-stage result."""

    academic_year: str
    engine: SolverEngineName
    # Final status after decode, post-processing, and invariant validation. A
    # heuristic post-process can produce a feasible final schedule even when
    # the underlying mathematical model was solved to optimality.
    status: SolverStatus
    solver_status: SolverStatus | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    wall_time_seconds: float | None = None
    solver_objective: float | None = None
    solver_best_bound: float | None = None
    postprocessed: bool = False
    source_instance_revision: int | None = Field(default=None, ge=1)
    metrics: ScheduleMetrics = Field(default_factory=ScheduleMetrics)
    validation_errors: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    diagnostics: list[SolverDiagnostic] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

class Schedule(StrictModel):
    meta: ScheduleMeta
    assignments: list[Assignment] = Field(default_factory=list)
    unassigned: list[str] = Field(
        default_factory=list,
        description="resident ids with no assignments",
    )

    @model_validator(mode="after")
    def assignments_do_not_overlap(self) -> "Schedule":
        seen: set[tuple[str, int]] = set()
        for assignment in self.assignments:
            for week in assignment.weeks:
                key = (assignment.resident_id, week)
                if key in seen:
                    raise ValueError(
                        f"overlapping assignments for {assignment.resident_id} in week {week}"
                    )
                seen.add(key)
        return self

    @property
    def week_grid(self) -> dict[str, dict[str, str]]:
        grid: dict[str, dict[str, str]] = {}
        for assignment in self.assignments:
            resident = grid.setdefault(assignment.resident_id, {})
            for week in assignment.weeks:
                resident[str(week)] = assignment.rotation_id
        return grid

    @property
    def elective_grid(self) -> dict[str, dict[str, bool]]:
        """Whether each assigned resident-week is Elective time."""
        grid: dict[str, dict[str, bool]] = {}
        for assignment in self.assignments:
            resident = grid.setdefault(assignment.resident_id, {})
            for week in assignment.weeks:
                resident[str(week)] = assignment.elective
        return grid

    def assignment_for(self, resident_id: str, week: int) -> Assignment | None:
        return next(
            (
                assignment
                for assignment in self.assignments
                if assignment.resident_id == resident_id and week in assignment.weeks
            ),
            None,
        )

    @property
    def coverage(self) -> list[WeekCoverage]:
        grouped: dict[tuple[int, str, bool], list[str]] = {}
        for assignment in self.assignments:
            for week in assignment.weeks:
                grouped.setdefault(
                    (week, assignment.rotation_id, assignment.elective),
                    [],
                ).append(assignment.resident_id)
        return [
            WeekCoverage(
                week=week,
                rotation_id=rotation_id,
                elective=elective,
                resident_ids=sorted(set(ids)),
            )
            for (week, rotation_id, elective), ids in sorted(grouped.items())
        ]

    def is_empty(self) -> bool:
        return not self.assignments
