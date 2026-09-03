import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.ingest import loads_instance
from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
from rbs.models.locks import LockedPlacement
from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
from rbs.ui.locks import (
    THROUGH_TODAY_SOURCE,
    ScheduleBlock,
    block_overlapping_lock_sources,
    clear_schedule_block,
    lock_schedule_block,
    replace_manual_block,
    schedule_blocks,
    schedule_gaps,
    set_lock_through_today,
    unlock_schedule_block,
)


def test_sample_includes_locks() -> None:
    instance = sample_instance()
    assert instance.locks
    clinic = instance.locks[0]
    assert clinic.resident_id == "resident-001"
    assert clinic.rotation_id == "clinic"
    assert clinic.weeks == [12, 13]


def test_rejects_lock_on_unknown_resident() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [{"resident_id": "nobody", "rotation_id": "icu", "weeks": [1, 2, 3, 4]}]
    with pytest.raises(ValidationError, match="unknown resident"):
        loads_instance(json.dumps(payload))


def test_rejects_lock_to_rotation_not_in_curriculum() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [{"resident_id": "resident-001", "rotation_id": "derm", "weeks": [1, 2]}]
    with pytest.raises(ValidationError, match="cannot take"):
        loads_instance(json.dumps(payload))


def test_rejects_vacation_lock_on_nonvacationable() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [{"resident_id": "resident-001", "rotation_id": "fmed", "weeks": [12]}]
    with pytest.raises(ValidationError, match="not vacationable"):
        loads_instance(json.dumps(payload))


def test_rejects_outpatient_gyn_lock_before_earliest_block() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [
        {
            "resident_id": "resident-001",
            "rotation_id": "outpatient_gyn",
            "weeks": [17, 18],
        }
    ]
    with pytest.raises(ValidationError, match=r"earliest block \(week 21\)"):
        loads_instance(json.dumps(payload))


def test_rejects_lock_before_rotation_earliest_week() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [
        {"resident_id": "resident-001", "rotation_id": "icu", "weeks": [1, 2, 3, 4]}
    ]
    with pytest.raises(ValidationError, match=r"earliest block \(week 5\)"):
        loads_instance(json.dumps(payload))


def test_rejects_two_rotations_same_week() -> None:
    payload = sample_instance().model_dump(mode="json")
    payload["locks"] = [
        {"resident_id": "resident-001", "rotation_id": "clinic", "weeks": [20]},
        {"resident_id": "resident-001", "rotation_id": "icu", "weeks": [20]},
    ]
    with pytest.raises(ValidationError, match="pinned to both"):
        loads_instance(json.dumps(payload))


def test_lock_weeks_are_sorted_unique() -> None:
    lock = LockedPlacement(resident_id="resident-001", rotation_id="icu", weeks=[22, 20, 21, 23])
    assert lock.weeks == [20, 21, 22, 23]
    assert lock.source == "manual"


def test_lock_through_today_locks_complete_started_rotations_only() -> None:
    instance = sample_instance()
    resident_id = instance.residents[0].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.NOT_IMPLEMENTED,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
            ),
            Assignment(
                resident_id=resident_id,
                rotation_id="icu",
                start_week=5,
                end_week=8,
                weeks=[5, 6, 7, 8],
            ),
            Assignment(
                resident_id=resident_id,
                rotation_id="elective",
                start_week=9,
                end_week=10,
                weeks=[9, 10],
            ),
        ],
    )
    today = instance.calendar.first_week_start + timedelta(weeks=5, days=2)

    updated = set_lock_through_today(instance, schedule, today, enabled=True)

    automatic = [lock for lock in updated.locks if lock.source == THROUGH_TODAY_SOURCE]
    assert updated.lock_through_today
    assert all(lock.exact_block for lock in automatic)
    assert [(lock.rotation_id, lock.weeks) for lock in automatic] == [
        ("fmed", [1, 2, 3, 4]),
        ("icu", [5, 6, 7, 8]),
    ]
    assert any(lock.source == "manual" for lock in updated.locks)

    disabled = set_lock_through_today(updated, schedule, today, enabled=False)
    assert not disabled.lock_through_today
    assert all(lock.source == "manual" for lock in disabled.locks)
    assert disabled.locks == instance.locks


def test_schedule_blocks_keep_rotation_group_members_independent() -> None:
    instance = sample_instance()
    resident_id = instance.residents[0].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="outpatient_gyn",
                start_week=21,
                end_week=22,
                weeks=[21, 22],
                block_start_week=21,
                block_duration_weeks=2,
            ),
            Assignment(
                resident_id=resident_id,
                rotation_id="inpatient_ld",
                start_week=23,
                end_week=24,
                weeks=[23, 24],
                block_start_week=23,
                block_duration_weeks=2,
            ),
        ],
    )

    assert schedule_blocks(schedule, resident_id=resident_id) == [
        ScheduleBlock(
            resident_id=resident_id,
            rotation_id="outpatient_gyn",
            start_week=21,
            duration_weeks=2,
        ),
        ScheduleBlock(
            resident_id=resident_id,
            rotation_id="inpatient_ld",
            start_week=23,
            duration_weeks=2,
        ),
    ]


def test_clear_schedule_block_removes_only_one_rotation_group_member() -> None:
    instance = sample_instance()
    resident_id = instance.residents[0].id
    other_resident_id = instance.residents[1].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="outpatient_gyn",
                start_week=21,
                end_week=22,
                weeks=[21, 22],
                block_start_week=21,
                block_duration_weeks=2,
            ),
            Assignment(
                resident_id=resident_id,
                rotation_id="inpatient_ld",
                start_week=23,
                end_week=24,
                weeks=[23, 24],
                block_start_week=23,
                block_duration_weeks=2,
            ),
            Assignment(
                resident_id=other_resident_id,
                rotation_id="elective",
                kind=RotationKind.ELECTIVE,
                start_week=21,
                end_week=22,
                weeks=[21, 22],
                block_start_week=21,
                block_duration_weeks=2,
            ),
        ],
    )
    block = schedule_blocks(schedule, resident_id=resident_id)[0]

    cleared = clear_schedule_block(schedule, block)

    assert [(item.resident_id, item.rotation_id) for item in cleared.assignments] == [
        (resident_id, "inpatient_ld"),
        (other_resident_id, "elective"),
    ]
    assert cleared.meta.status is SolverStatus.UNKNOWN
    assert cleared.meta.solver_status is SolverStatus.UNKNOWN
    assert cleared.meta.notes[-1].endswith("solve required")
    assert schedule_gaps(
        cleared,
        resident_id=resident_id,
        calendar_weeks=instance.calendar.weeks,
    ) == [list(range(1, 23)), list(range(25, instance.calendar.weeks + 1))]


def test_incomplete_schedule_defers_missing_prerequisite_to_the_next_solve() -> None:
    from rbs.solver.validation import validate_schedule

    instance = sample_instance()
    resident = instance.residents[0]
    incomplete = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.UNKNOWN,
            solver_status=SolverStatus.UNKNOWN,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="icu",
                start_week=19,
                end_week=22,
                weeks=[19, 20, 21, 22],
                block_start_week=19,
                block_duration_weeks=4,
            )
        ],
    )

    errors = validate_schedule(instance, incomplete).errors

    assert not any("before a completed fmed block" in error for error in errors)


def test_manual_exact_block_can_be_added_locked_and_unlocked() -> None:
    instance = sample_instance()
    resident_id = instance.residents[0].id
    updated = replace_manual_block(
        instance,
        resident_id=resident_id,
        rotation_id="icu",
        start_week=20,
        duration_weeks=4,
    )
    exact = updated.locks[-1]
    assert exact.exact_block
    assert exact.weeks == [20, 21, 22, 23]

    block = ScheduleBlock(resident_id, "icu", 20, 4)
    assert lock_schedule_block(updated, block) == updated
    unlocked = unlock_schedule_block(updated, block)
    assert all(
        lock.source != "manual"
        or lock.resident_id != resident_id
        or not set(lock.weeks) & set(block.weeks)
        for lock in unlocked.locks
    )


def test_manual_exact_block_can_explicitly_exempt_rotation_grouping() -> None:
    instance = sample_instance()

    updated = replace_manual_block(
        instance,
        resident_id="resident-001",
        rotation_id="outpatient_gyn",
        start_week=21,
        duration_weeks=2,
        grouping_exempt=True,
    )

    lock = updated.locks[-1]
    assert lock.exact_block
    assert lock.grouping_exempt


def test_grouping_exemption_rejects_ungrouped_rotation() -> None:
    instance = sample_instance()

    with pytest.raises(ValidationError, match="grouped Mandatory rotation"):
        replace_manual_block(
            instance,
            resident_id="resident-001",
            rotation_id="icu",
            start_week=21,
            duration_weeks=4,
            grouping_exempt=True,
        )


def test_partial_week_pin_still_marks_the_source_block_as_locked() -> None:
    instance = sample_instance()
    block = ScheduleBlock("resident-001", "clinic", 12, 4)

    assert block_overlapping_lock_sources(instance, block) == {"manual"}


def test_manual_unlock_keeps_automatic_overlay_until_setting_is_disabled() -> None:
    instance = sample_instance()
    resident_id = instance.residents[8].id
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="fmed",
                kind=RotationKind.FMED,
                start_week=1,
                end_week=4,
                weeks=[1, 2, 3, 4],
                block_start_week=1,
                block_duration_weeks=4,
            )
        ],
    )
    today = instance.calendar.first_week_start + timedelta(weeks=3)
    automatic = set_lock_through_today(instance, schedule, today, enabled=True)
    block = schedule_blocks(schedule)[0]

    manually_unlocked = unlock_schedule_block(automatic, block)
    assert any(
        lock.source == THROUGH_TODAY_SOURCE and lock.weeks == [1, 2, 3, 4]
        for lock in manually_unlocked.locks
    )
    disabled = set_lock_through_today(
        manually_unlocked,
        schedule,
        today,
        enabled=False,
    )
    assert not any(
        lock.resident_id == resident_id and set(lock.weeks) & {1, 2, 3, 4}
        for lock in disabled.locks
    )
