"""Unscheduled curriculum time: the weeks a training level has not yet spent.

A curriculum is built up rather than swapped. A level starts with every calendar
week unscheduled and allocates them to Mandatory, Clinic, and Elective
requirements; solving is what requires the total to reach the calendar.
"""

import pytest
from pydantic import ValidationError

from rbs.catalog import blank_instance, sample_instance
from rbs.models.enums import RotationKind
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.resident import Resident
from rbs.models.rotation import Rotation
from rbs.solver.readiness import check_solve_readiness, unallocated_weeks_by_level
from rbs.training_levels import add_training_level
from rbs.ui.rotations.ops import (
    add_elective_rotation,
    add_mandatory_rotation,
    direct_elective_counts,
    elective_slot_rotation,
    remove_mandatory_rotation,
    replace_standard_rotation,
    set_elective_allocation,
)


def _mandatory(rotation_id: str, code: str, pgy: int, duration: int) -> Rotation:
    return Rotation.model_validate(
        {
            "id": rotation_id,
            "code": code,
            "name": rotation_id.replace("_", " ").title(),
            "kind": RotationKind.STANDARD.value,
            "max_consecutive_weeks": max(duration, 4),
            "pgy_rules": [
                {
                    "pgy": pgy,
                    "block_configs": [{"duration_weeks": duration}],
                }
            ],
        }
    )


def _elective(rotation_id: str, code: str, pgy: int, duration: int) -> Rotation:
    return Rotation.model_validate(
        {
            "id": rotation_id,
            "code": code,
            "name": rotation_id.replace("_", " ").title(),
            "kind": RotationKind.ELECTIVE.value,
            "pgy_rules": [
                {
                    "pgy": pgy,
                    "block_configs": [{"duration_weeks": duration}],
                }
            ],
        }
    )


# ---- unscheduled weeks -------------------------------------------------


def test_a_new_workspace_starts_with_every_week_unscheduled() -> None:
    instance = blank_instance()

    assert instance.unallocated_weeks(1) == instance.calendar.weeks
    assert not instance.curriculum_for(1).blocks


def test_a_new_training_level_starts_with_every_week_unscheduled() -> None:
    instance = add_training_level(sample_instance(), code="PGY4", label="PGY 4")
    added = max(instance.training_level_ids)

    assert instance.unallocated_weeks(added) == instance.calendar.weeks


def test_a_partly_built_curriculum_is_valid() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 3})

    assert instance.curriculum_for(1).required_weeks() == 6
    assert instance.unallocated_weeks(1) == 46


def test_a_curriculum_cannot_exceed_the_calendar() -> None:
    raw = blank_instance().model_dump(mode="json")
    raw["rotations"].append(_elective("elective", "ELEC", 1, 5).model_dump(mode="json"))
    raw["requirements"][0]["blocks"] = [
        {"rotation_id": "elective", "duration_weeks": 5, "count": 11}
    ]

    with pytest.raises(ValidationError, match="exceeding the 52-week calendar"):
        SchedulerInput.model_validate(raw)


# ---- spending and releasing weeks --------------------------------------


def test_a_mandatory_rotation_can_be_added_to_a_new_workspace() -> None:
    instance = blank_instance()

    added = add_mandatory_rotation(instance, _mandatory("wards", "WRD", 1, 4), {(1, 4): 3})

    assert added.curriculum_for(1).required_weeks() == 12
    assert added.unallocated_weeks(1) == 40


def test_a_requirement_larger_than_the_calendar_is_refused() -> None:
    instance = blank_instance()
    rotation = _mandatory("wards", "WRD", 1, 4)

    with pytest.raises(
        ValueError,
        match="only 52 unscheduled and 0 direct Elective weeks",
    ):
        add_mandatory_rotation(instance, rotation, {(1, 4): 15})


# ---- funding a requirement from Elective time --------------------------


def test_a_full_curriculum_funds_a_requirement_from_elective_time() -> None:
    instance = sample_instance()
    assert instance.unallocated_weeks(1) == 0
    assert direct_elective_counts(instance, 1) == {2: 1}

    added = add_mandatory_rotation(
        instance,
        _mandatory("addiction_medicine", "ADDICT", 1, 2),
        {(1, 2): 1},
    )

    assert direct_elective_counts(added, 1) == {}
    assert added.curriculum_for(1).required_weeks() == 52
    assert added.unallocated_weeks(1) == 0


def test_funding_reshapes_electives_so_any_length_fits() -> None:
    """Eight 2-week Elective weeks can fund a 3-week block by re-splitting them."""
    instance = sample_instance()
    assert direct_elective_counts(instance, 2) == {2: 4}

    added = add_mandatory_rotation(
        instance,
        _mandatory("addiction_medicine", "ADDICT", 2, 3),
        {(2, 3): 1},
    )

    assert direct_elective_counts(added, 2) == {1: 1, 2: 2}
    assert added.curriculum_for(2).required_weeks() == 52


def test_unscheduled_weeks_are_spent_before_elective_time() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 3})
    assert instance.unallocated_weeks(1) == 46

    added = add_mandatory_rotation(instance, _mandatory("wards", "WRD", 1, 4), {(1, 4): 4})

    assert direct_elective_counts(added, 1) == {2: 3}
    assert added.unallocated_weeks(1) == 30


def test_funding_falls_back_to_electives_only_for_the_shortfall() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 4})
    assert instance.unallocated_weeks(1) == 44

    added = add_mandatory_rotation(instance, _mandatory("wards", "WRD", 1, 5), {(1, 5): 9})

    # 45 weeks needed: 44 unscheduled, then 1 week out of the 8 Elective weeks.
    assert direct_elective_counts(added, 1) == {2: 3, 1: 1}
    assert added.curriculum_for(1).required_weeks() == 52
    assert added.unallocated_weeks(1) == 0


def test_a_requirement_larger_than_both_pools_is_refused() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 2})

    # 56 weeks needed against 48 unscheduled + 4 Elective.
    with pytest.raises(
        ValueError,
        match="only 48 unscheduled and 4 direct Elective weeks",
    ):
        add_mandatory_rotation(instance, _mandatory("wards", "WRD", 1, 4), {(1, 4): 14})


def test_removing_a_mandatory_rotation_returns_its_weeks_unscheduled() -> None:
    instance = add_mandatory_rotation(
        blank_instance(),
        _mandatory("wards", "WRD", 1, 4),
        {(1, 4): 3},
    )

    removed = remove_mandatory_rotation(instance, "wards")

    assert removed.unallocated_weeks(1) == 52
    assert not removed.curriculum_for(1).blocks


def test_any_block_length_fits_while_weeks_remain() -> None:
    """Allocation is a week budget, not a subset sum over fixed elective shapes."""
    instance = blank_instance()

    for duration in range(1, 6):
        added = add_mandatory_rotation(
            instance,
            _mandatory(f"rotation_{duration}", f"R{duration}", 1, duration),
            {(1, duration): 1},
        )
        assert added.unallocated_weeks(1) == 52 - duration


# ---- elective allocation -----------------------------------------------


def test_elective_allocation_creates_the_slot_rotation_on_demand() -> None:
    instance = blank_instance()
    assert elective_slot_rotation(instance) is None

    allocated = set_elective_allocation(instance, 1, {2: 4})

    slot = elective_slot_rotation(allocated)
    assert slot is not None
    assert slot.kind is RotationKind.ELECTIVE
    assert direct_elective_counts(allocated, 1) == {2: 4}
    assert allocated.unallocated_weeks(1) == 44


def test_elective_allocation_chooses_its_own_block_sizes() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {1: 2, 3: 2})

    assert direct_elective_counts(instance, 1) == {1: 2, 3: 2}
    assert instance.unallocated_weeks(1) == 44


def test_elective_allocation_releases_weeks_when_reduced() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 6})

    reduced = set_elective_allocation(instance, 1, {2: 1})

    assert reduced.unallocated_weeks(1) == 50
    assert direct_elective_counts(reduced, 1) == {2: 1}


def test_elective_allocation_cannot_exceed_unscheduled_weeks() -> None:
    instance = blank_instance()

    with pytest.raises(ValueError, match="unscheduled weeks remain"):
        set_elective_allocation(instance, 1, {4: 20})


def test_adding_an_elective_service_allocates_the_time_it_can_fill() -> None:
    """The chicken-and-egg an option's block sizes used to create."""
    instance = blank_instance()

    added = add_elective_rotation(instance, _elective("geriatrics", "GERI", 1, 2))

    option = added.electives.option_for("geriatrics")
    assert option is not None
    assert option.eligible_block_sizes == [2]
    assert direct_elective_counts(added, 1) == {2: 1}
    assert added.unallocated_weeks(1) == 50


def test_adding_an_elective_service_reuses_existing_slots() -> None:
    instance = set_elective_allocation(blank_instance(), 1, {2: 5})

    added = add_elective_rotation(instance, _elective("geriatrics", "GERI", 1, 2))

    assert direct_elective_counts(added, 1) == {2: 5}
    assert added.unallocated_weeks(1) == 42


# ---- solve readiness ---------------------------------------------------


def _with_resident(instance: SchedulerInput) -> SchedulerInput:
    return instance.revised(
        residents=[
            Resident.model_validate(
                {"id": "resident-001", "name": "Test Resident", "pgy": 1}
            )
        ]
    )


def test_a_fully_allocated_workspace_is_ready_to_solve() -> None:
    result = check_solve_readiness(SolverProblem.from_instance(sample_instance()))

    assert result.ready
    assert not unallocated_weeks_by_level(SolverProblem.from_instance(sample_instance()))


def test_unscheduled_weeks_block_a_solve_in_the_program_s_words() -> None:
    instance = _with_resident(set_elective_allocation(blank_instance(), 1, {2: 4}))

    result = check_solve_readiness(SolverProblem.from_instance(instance))

    assert not result.ready
    assert result.errors == ("PGY1 has 44 unscheduled weeks to allocate",)


def test_named_additions_can_fill_each_residents_unallocated_time() -> None:
    instance = sample_instance()
    rotation = instance.rotation("night_float")
    freed = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        counts={(1, 2): 0, (2, 2): 0},
    )
    assert freed.unallocated_weeks(1) == 2
    pgy1_resident_ids = [resident.id for resident in freed.residents if resident.pgy == 1]

    partially_filled = replace_standard_rotation(
        freed,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": pgy1_resident_ids[0],
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
        ],
    )
    partial_readiness = check_solve_readiness(SolverProblem.from_instance(partially_filled))
    assert any(issue.code == "unallocated_weeks" for issue in partial_readiness.issues)

    filled = replace_standard_rotation(
        freed,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": resident_id,
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
            for resident_id in pgy1_resident_ids
        ],
    )

    assert all(
        filled.resident_unallocated_weeks(resident_id) == 0 for resident_id in pgy1_resident_ids
    )
    assert check_solve_readiness(SolverProblem.from_instance(filled)).ready


def test_waived_requirement_becomes_resident_unallocated_time() -> None:
    instance = sample_instance()
    rotation = instance.rotation("night_float")
    waiver = {
        "resident_id": "resident-001",
        "rotation_id": rotation.id,
        "duration_weeks": 2,
    }
    waived = replace_standard_rotation(
        instance,
        rotation.id,
        rotation,
        resident_waivers=[waiver],
    )

    assert waived.resident_unallocated_weeks("resident-001") == 2
    assert not check_solve_readiness(SolverProblem.from_instance(waived)).ready

    filled = replace_standard_rotation(
        waived,
        rotation.id,
        rotation,
        resident_overrides=[
            {
                "resident_id": "resident-001",
                "rotation_id": rotation.id,
                "duration_weeks": 2,
                "replaces_rotation_id": None,
            }
        ],
    )

    assert filled.resident_unallocated_weeks("resident-001") == 0
    assert check_solve_readiness(SolverProblem.from_instance(filled)).ready


def test_a_level_with_no_residents_never_blocks_a_solve() -> None:
    instance = add_training_level(sample_instance(), code="PGY4", label="PGY 4")

    result = check_solve_readiness(SolverProblem.from_instance(instance))

    assert result.ready


def test_missing_elective_fallback_is_reported_before_solving() -> None:
    raw = sample_instance().model_dump(mode="json")
    clinic = next(rotation for rotation in raw["rotations"] if rotation["id"] == "clinic")
    pgy1_rule = next(rule for rule in clinic["pgy_rules"] if rule["pgy"] == 1)
    pgy1_rule["block_configs"] = [
        {
            "duration_weeks": 4,
            "vacation": {"allowed": True, "max_weeks_per_block": 1},
        }
    ]
    pgy1 = next(curriculum for curriculum in raw["requirements"] if curriculum["pgy"] == 1)
    pgy1["blocks"] = [
        block
        for block in pgy1["blocks"]
        if not (block["rotation_id"] == "clinic" and block["duration_weeks"] == 2)
    ]
    pgy1["blocks"].append({"rotation_id": "clinic", "duration_weeks": 4, "count": 1})
    instance = SchedulerInput.model_validate(raw)

    result = check_solve_readiness(SolverProblem.from_instance(instance))

    issue = next(issue for issue in result.issues if issue.code == "missing_elective_fallback")
    assert "Clinic · PGY1" in issue.message
    assert "2-week Elective" in issue.message
    assert "2-week Clinic block configuration" in issue.suggestions[0]
    assert issue.pgy == 1


def test_impossible_weekly_minimum_explains_bounds_and_available_weeks() -> None:
    raw = sample_instance().model_dump(mode="json")
    rotation = next(
        rotation for rotation in raw["rotations"] if rotation["id"] == "inpatient_ld"
    )
    rotation["capacity"]["max_concurrent"] = 1
    pgy2_rule = next(rule for rule in rotation["pgy_rules"] if rule["pgy"] == 2)
    pgy2_rule["min_concurrent"] = 2
    pgy2_rule["max_concurrent"] = None
    instance = SchedulerInput.model_validate(raw)

    result = check_solve_readiness(SolverProblem.from_instance(instance))

    issue = next(issue for issue in result.issues if issue.rotation_id == "inpatient_ld")
    assert issue.code == "rotation_capacity_conflict"
    assert "minimums total 2 residents every week" in issue.message
    assert "overall maximum of 1" in issue.message
    assert "needs 104 resident-weeks" in issue.message
    assert "at most 16 are available" in issue.message


def test_required_block_longer_than_consecutive_limit_is_reported() -> None:
    raw = sample_instance().model_dump(mode="json")
    rotation = next(rotation for rotation in raw["rotations"] if rotation["id"] == "icu")
    rotation["max_consecutive_weeks"] = 2
    instance = SchedulerInput.model_validate(raw)

    result = check_solve_readiness(SolverProblem.from_instance(instance))

    issue = next(issue for issue in result.issues if issue.rotation_id == "icu")
    assert issue.code == "block_exceeds_consecutive_limit"
    assert "ICU · PGY1" in issue.message
    assert "required 4-week block" in issue.message
    assert "2-week maximum consecutive limit" in issue.message
    assert issue.pgy == 1
