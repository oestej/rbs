"""Turning a schedule into a working draft for the current inputs.

Editing a schedule whose inputs have changed must not depend on a solve first,
and must not lose work the user could get back by undoing that input change.
So work that only stopped fitting stays in the draft, hidden until the next
solve (see :mod:`rbs.solver.validation_inherited`). The draft gives up only what
saved data can no longer point at: work for a resident, rotation, block length,
attending, or clinic that no longer exists. A clinic session outlives its clinic
and loses only the site, because the half-day itself is still meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass

from rbs.models.enums import SolverStatus
from rbs.models.instance import SolverProblem
from rbs.models.schedule import (
    AssignedAttendingWork,
    AssignedClinic,
    Assignment,
    Schedule,
    ScheduleMetrics,
)
from rbs.solver.validation_assignments import _validate_assignments
from rbs.solver.validation_inherited import InheritedWork


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


@dataclass(frozen=True, slots=True)
class LeftOutOfDraft:
    """Work a working draft could not keep because what it belongs to is gone."""

    blocks: int = 0
    clinic_sessions: int = 0
    attending_half_days: int = 0

    @property
    def message(self) -> str | None:
        """What to tell the user, in program terms, or ``None`` if nothing was left out."""
        parts = [
            _counted(count, noun)
            for count, noun in (
                (self.blocks, "block"),
                (self.clinic_sessions, "clinic session"),
                (self.attending_half_days, "attending half-day"),
            )
            if count
        ]
        if not parts:
            return None
        listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
        return (
            f"{listed} left out of this working draft because the resident, rotation, "
            "block length, attending, or clinic was removed."
        )


def _without_removed_sites(instance: SolverProblem, assignment: Assignment) -> Assignment:
    """Keep each session at its half-day, forgetting only sites that no longer exist."""
    policy = instance.clinic_policy
    site_ids = set(policy.site_ids)
    slots: list[AssignedClinic] = []
    changed = False
    for slot in assignment.clinic_slots:
        updates: dict = {}
        if slot.site is not None and slot.site not in site_ids:
            updates["site"] = None
        if (
            slot.manual_override_original_site is not None
            and slot.manual_override_original_site not in site_ids
        ):
            updates["manual_override_original_site"] = None
        allowed = policy.resolve_site_ids(slot.allowed_sites)
        if any(site not in site_ids for site in allowed):
            updates["allowed_sites"] = [site for site in allowed if site in site_ids]
        changed = changed or bool(updates)
        slots.append(slot.model_copy(update=updates) if updates else slot)
    return assignment.revised(clinic_slots=slots) if changed else assignment


def _reconciled(
    instance: SolverProblem,
    schedule: Schedule,
) -> tuple[list[Assignment], list[AssignedAttendingWork], list[str], LeftOutOfDraft]:
    inherited = InheritedWork.of(schedule)
    expected_weeks = set(range(1, instance.calendar.weeks + 1))
    assignments: list[Assignment] = []
    blocks = clinic_sessions = 0
    for assignment in schedule.assignments:
        candidate = _without_removed_sites(instance, assignment)
        # The check a saved draft must pass, so the two cannot disagree.
        errors: list[str] = []
        _validate_assignments(
            instance,
            schedule.model_copy(update={"assignments": [candidate]}),
            expected_weeks,
            {},
            {},
            errors,
            inherited=inherited,
        )
        if errors:
            blocks += 1
            clinic_sessions += sum(not slot.admin for slot in assignment.clinic_slots)
        else:
            assignments.append(candidate)
    attendings = {attending.id for attending in instance.attendings}
    site_ids = set(instance.clinic_policy.site_ids)
    attending_work = [
        item
        for item in schedule.attending_work
        if item.attending_id in attendings
        and item.week <= instance.calendar.weeks
        and (item.clinic_id is None or item.clinic_id in site_ids)
    ]
    unassigned = [
        resident_id
        for resident_id in schedule.unassigned
        if resident_id in instance.residents_by_id
    ]
    return (
        assignments,
        attending_work,
        unassigned,
        LeftOutOfDraft(
            blocks=blocks,
            clinic_sessions=clinic_sessions,
            attending_half_days=len(schedule.attending_work) - len(attending_work),
        ),
    )


def left_out_of_draft(instance: SolverProblem, schedule: Schedule) -> LeftOutOfDraft:
    """What making ``schedule`` a working draft for these inputs would give up."""
    return _reconciled(instance, schedule)[3]


def working_draft_for_inputs(instance: SolverProblem, schedule: Schedule) -> Schedule:
    """Mark a schedule as an unsolved draft of the current inputs, keeping all it can."""
    if schedule.meta.academic_year != instance.academic_year:
        raise ValueError("This schedule belongs to a different academic year. Reopen the schedule.")
    assignments, attending_work, unassigned, left_out = _reconciled(instance, schedule)
    notes = list(schedule.meta.notes)
    if left_out.message is not None and left_out.message not in notes:
        notes.append(left_out.message)
    return schedule.revised(
        assignments=assignments,
        attending_work=attending_work,
        unassigned=unassigned,
        meta=schedule.meta.revised(
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
            solver_objective=None,
            solver_best_bound=None,
            metrics=ScheduleMetrics(),
            validation_errors=[],
            validation_warnings=[],
            diagnostics=[],
            notes=notes,
        ),
    )


__all__ = ["LeftOutOfDraft", "left_out_of_draft", "working_draft_for_inputs"]
