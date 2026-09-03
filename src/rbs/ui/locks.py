"""Schedule-aware lock generation for the workspace UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from rbs.models.enums import SolverStatus
from rbs.models.instance import SchedulerInput
from rbs.models.locks import LockedPlacement
from rbs.models.schedule import Assignment, Schedule, ScheduleMetrics

THROUGH_TODAY_SOURCE = "through_today"


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    """One complete scheduled rotation block."""

    resident_id: str
    rotation_id: str
    start_week: int
    duration_weeks: int
    elective: bool = False

    @property
    def end_week(self) -> int:
        return self.start_week + self.duration_weeks - 1

    @property
    def weeks(self) -> list[int]:
        return list(range(self.start_week, self.end_week + 1))

    @property
    def key(self) -> str:
        return (
            f"{self.resident_id}:{self.rotation_id}:"
            f"{self.start_week}:{self.duration_weeks}:{int(self.elective)}"
        )


def assignment_start_date(instance: SchedulerInput, assignment: Assignment) -> date:
    """Return the Monday on which an assignment begins."""
    return instance.calendar.first_week_start + timedelta(weeks=assignment.start_week - 1)


def block_start_date(instance: SchedulerInput, block: ScheduleBlock) -> date:
    """Return the Monday on which a complete source block begins."""
    return instance.calendar.first_week_start + timedelta(weeks=block.start_week - 1)


def schedule_blocks(
    schedule: Schedule | None,
    *,
    resident_id: str | None = None,
) -> list[ScheduleBlock]:
    """Return chronological scheduled blocks."""
    if schedule is None:
        return []
    blocks: dict[tuple[str, str, int, int, bool], ScheduleBlock] = {}
    for assignment in schedule.assignments:
        if resident_id is not None and assignment.resident_id != resident_id:
            continue
        start_week = assignment.block_start_week or assignment.start_week
        duration_weeks = assignment.block_duration_weeks or len(assignment.weeks)
        key = (
            assignment.resident_id,
            assignment.rotation_id,
            start_week,
            duration_weeks,
            assignment.elective,
        )
        blocks[key] = ScheduleBlock(
            resident_id=assignment.resident_id,
            rotation_id=assignment.rotation_id,
            start_week=start_week,
            duration_weeks=duration_weeks,
            elective=assignment.elective,
        )
    return sorted(
        blocks.values(),
        key=lambda block: (
            block.resident_id,
            block.start_week,
            block.rotation_id,
            block.duration_weeks,
            block.elective,
        ),
    )


def schedule_gaps(
    schedule: Schedule | None,
    *,
    resident_id: str,
    calendar_weeks: int,
) -> list[list[int]]:
    """Return contiguous unscheduled week ranges for one resident."""
    if schedule is None:
        return []
    assigned = {int(week) for week in schedule.week_grid.get(resident_id, {})}
    gaps: list[list[int]] = []
    for week in range(1, calendar_weeks + 1):
        if week in assigned:
            continue
        if gaps and gaps[-1][-1] == week - 1:
            gaps[-1].append(week)
        else:
            gaps.append([week])
    return gaps


def clear_schedule_block(
    schedule: Schedule,
    block: ScheduleBlock,
) -> Schedule:
    """Remove one populated source block and mark the schedule as incomplete."""
    kept: list[Assignment] = []
    removed = False
    for assignment in schedule.assignments:
        belongs_to_block = (
            assignment.resident_id == block.resident_id
            and assignment.rotation_id == block.rotation_id
            and assignment.elective == block.elective
            and (assignment.block_start_week or assignment.start_week) == block.start_week
            and (assignment.block_duration_weeks or len(assignment.weeks)) == block.duration_weeks
        )
        if belongs_to_block:
            removed = True
            continue
        kept.append(assignment)
    if not removed:
        raise ValueError("the scheduled block no longer exists")

    note = (
        f"Manual override cleared {block.resident_id} weeks "
        f"{block.start_week}-{block.end_week}; solve required"
    )
    meta = schedule.meta.model_copy(
        update={
            "status": SolverStatus.UNKNOWN,
            "solver_status": SolverStatus.UNKNOWN,
            "metrics": ScheduleMetrics(),
            "validation_errors": [],
            "validation_warnings": [],
            "notes": [*schedule.meta.notes, note],
        }
    )
    return schedule.model_copy(update={"assignments": kept, "meta": meta})


def exact_block_lock(
    resident_id: str,
    rotation_id: str,
    start_week: int,
    duration_weeks: int,
    *,
    source: str = "manual",
    elective: bool = False,
    grouping_exempt: bool = False,
) -> LockedPlacement:
    """Build a lock that fixes one exact curriculum block placement."""
    if duration_weeks < 1:
        raise ValueError("block duration must be at least one week")
    return LockedPlacement(
        resident_id=resident_id,
        rotation_id=rotation_id,
        elective=elective,
        weeks=list(range(start_week, start_week + duration_weeks)),
        source=source,
        exact_block=True,
        grouping_exempt=grouping_exempt,
    )


def _with_locks(
    instance: SchedulerInput,
    locks: list[LockedPlacement],
) -> SchedulerInput:
    return instance.revised(locks=locks)


def block_lock_sources(
    instance: SchedulerInput,
    block: ScheduleBlock,
) -> set[str]:
    """Return sources that cover every week of ``block`` on its rotation."""
    sources: set[str] = set()
    expected = set(block.weeks)
    for source in ("manual", THROUGH_TODAY_SOURCE):
        covered = {
            week
            for lock in instance.locks
            if lock.source == source
            and lock.resident_id == block.resident_id
            and lock.rotation_id == block.rotation_id
            and lock.elective == block.elective
            for week in lock.weeks
        }
        if expected <= covered:
            sources.add(source)
    return sources


def block_overlapping_lock_sources(
    instance: SchedulerInput,
    block: ScheduleBlock,
) -> set[str]:
    """Return lock sources touching any week of ``block``."""
    target = set(block.weeks)
    return {
        lock.source
        for lock in instance.locks
        if lock.resident_id == block.resident_id and bool(target & set(lock.weeks))
    }


def lock_schedule_block(
    instance: SchedulerInput,
    block: ScheduleBlock,
) -> SchedulerInput:
    """Manually lock one existing schedule block without duplicating coverage."""
    if "manual" in block_lock_sources(instance, block):
        return instance
    return _with_locks(
        instance,
        [
            *instance.locks,
            exact_block_lock(
                block.resident_id,
                block.rotation_id,
                block.start_week,
                block.duration_weeks,
                elective=block.elective,
            ),
        ],
    )


def unlock_schedule_block(
    instance: SchedulerInput,
    block: ScheduleBlock,
) -> SchedulerInput:
    """Remove manual coverage for one block while retaining automatic locks."""
    target_weeks = set(block.weeks)
    locks: list[LockedPlacement] = []
    for lock in instance.locks:
        if lock.source != "manual" or lock.resident_id != block.resident_id:
            locks.append(lock)
            continue
        remaining = [week for week in lock.weeks if week not in target_weeks]
        if not remaining:
            continue
        locks.append(
            lock.model_copy(
                update={
                    "weeks": remaining,
                    # Removing part of an exact block turns any remaining pins
                    # back into ordinary week-level locks.
                    "exact_block": False,
                    "grouping_exempt": False,
                }
            )
        )
    return _with_locks(instance, locks)


def lock_resident_schedule(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
) -> SchedulerInput:
    """Manually lock every solved block for one resident."""
    updated = instance
    for block in schedule_blocks(schedule, resident_id=resident_id):
        updated = lock_schedule_block(updated, block)
    return updated


def unlock_resident_schedule(
    instance: SchedulerInput,
    resident_id: str,
) -> SchedulerInput:
    """Remove all of a resident's manual locks, preserving the auto overlay."""
    return _with_locks(
        instance,
        [
            lock
            for lock in instance.locks
            if lock.source != "manual" or lock.resident_id != resident_id
        ],
    )


def replace_manual_block(
    instance: SchedulerInput,
    *,
    resident_id: str,
    rotation_id: str,
    start_week: int,
    duration_weeks: int,
    elective: bool = False,
    original: LockedPlacement | None = None,
    replace_weeks: list[int] | None = None,
    grouping_exempt: bool = False,
) -> SchedulerInput:
    """Add or edit one exact resident block and validate it as solver input.

    ``replace_weeks`` is used when editing a solved block: any existing manual
    pins on those old weeks are cleared before the replacement is installed.
    Automatic locks are never changed here.
    """
    target = set(replace_weeks or [])
    removed_original = False
    kept: list[LockedPlacement] = []
    for lock in instance.locks:
        if (
            original is not None
            and not removed_original
            and lock.source == "manual"
            and lock == original
        ):
            removed_original = True
            continue
        if lock.source == "manual" and lock.resident_id == resident_id and target:
            remaining = [week for week in lock.weeks if week not in target]
            if not remaining:
                continue
            lock = lock.model_copy(
                update={
                    "weeks": remaining,
                    "exact_block": False,
                    "grouping_exempt": False,
                }
            )
        kept.append(lock)
    if original is not None and not removed_original:
        raise ValueError("the hardcoded block no longer exists")
    kept.append(
        exact_block_lock(
            resident_id,
            rotation_id,
            start_week,
            duration_weeks,
            elective=elective,
            grouping_exempt=grouping_exempt,
        )
    )
    return _with_locks(instance, kept)


def remove_manual_lock(
    instance: SchedulerInput,
    target: LockedPlacement,
) -> SchedulerInput:
    """Remove one manual lock by value without disturbing any overlay lock."""
    if target.source != "manual":
        raise ValueError("automatic locks are controlled from Settings")
    removed = False
    kept: list[LockedPlacement] = []
    for lock in instance.locks:
        if not removed and lock.source == "manual" and lock == target:
            removed = True
            continue
        kept.append(lock)
    if not removed:
        raise ValueError("the manual lock no longer exists")
    return _with_locks(instance, kept)


def automatic_locks_through_today(
    instance: SchedulerInput,
    schedule: Schedule | None,
    today: date,
) -> list[LockedPlacement]:
    """Lock every solved rotation that has begun on or before ``today``.

    An in-progress assignment is locked for its complete span so a later solve
    cannot move the remainder of the block.
    """
    if schedule is None:
        return []
    return [
        exact_block_lock(
            block.resident_id,
            block.rotation_id,
            block.start_week,
            block.duration_weeks,
            source=THROUGH_TODAY_SOURCE,
            elective=block.elective,
        )
        for block in schedule_blocks(schedule)
        if block_start_date(instance, block) <= today
    ]


def set_lock_through_today(
    instance: SchedulerInput,
    schedule: Schedule | None,
    today: date,
    *,
    enabled: bool,
) -> SchedulerInput:
    """Enable or disable schedule-derived locks while preserving manual locks."""
    manual = [lock for lock in instance.locks if lock.source != THROUGH_TODAY_SOURCE]
    automatic = automatic_locks_through_today(instance, schedule, today) if enabled else []
    return instance.revised(
        locks=[*manual, *automatic],
        lock_through_today=enabled,
    )


def refresh_locks_through_today(
    instance: SchedulerInput,
    schedule: Schedule | None,
    today: date,
) -> SchedulerInput:
    """Refresh automatic locks when their persistent checkbox is enabled."""
    if not instance.lock_through_today:
        return instance
    return set_lock_through_today(instance, schedule, today, enabled=True)
