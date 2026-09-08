"""Deterministic tests for shared lock-conflict infeasibility diagnostics.

These checks are pure Python over the validated instance: they never start a
CP-SAT search, so they stay outside the ``solve`` marker group.
"""

from rbs.catalog import sample_instance
from rbs.models.elective import ElectiveRotationOption
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.locks import LockedPlacement
from rbs.solver.core.diagnostics import (
    _locked_capacity_conflicts,
    _locked_elective_repeats,
)


def _problem(instance: SchedulerInput) -> SolverProblem:
    return SolverProblem.from_instance(instance)


def _pin(
    resident_id: str, rotation_id: str, weeks: list[int], *, elective: bool = False
) -> LockedPlacement:
    return LockedPlacement(
        resident_id=resident_id,
        rotation_id=rotation_id,
        elective=elective,
        weeks=weeks,
    )


def test_two_locked_residents_over_rotation_maximum_are_named() -> None:
    instance = sample_instance().revised(
        locks=[
            _pin("resident-001", "icu", [5, 6]),
            _pin("resident-002", "icu", [5, 6]),
        ]
    )
    diagnostics = _locked_capacity_conflicts(_problem(instance))
    assert len(diagnostics) == 1
    (diagnostic,) = diagnostics
    assert diagnostic.code == "locked_capacity_conflict"
    assert diagnostic.resident_ids == ["resident-001", "resident-002"]
    assert diagnostic.weeks == [5, 6]
    assert "Avery Chen" in diagnostic.message
    assert "Jordan Patel" in diagnostic.message
    assert "at most 1" in diagnostic.message
    assert diagnostic.suggestions


def test_locked_capacity_within_maximum_is_silent() -> None:
    instance = sample_instance().revised(locks=[_pin("resident-001", "icu", [5, 6])])
    assert _locked_capacity_conflicts(_problem(instance)) == []


def test_identical_duplicate_locks_count_as_one_resident() -> None:
    # Ethan Bailey's shape: the same week pin stored twice. The duplicate is
    # redundant but satisfiable, so the capacity check must stay silent.
    instance = sample_instance().revised(
        locks=[
            _pin("resident-001", "icu", [5, 6]),
            _pin("resident-001", "icu", [5, 6]),
        ]
    )
    assert _locked_capacity_conflicts(_problem(instance)) == []


def test_training_level_maximum_is_checked_independently() -> None:
    raw = sample_instance().model_dump(mode="json")
    for rotation in raw["rotations"]:
        if rotation["id"] == "outpatient_gyn":
            rotation["capacity"]["max_concurrent"] = 2
    instance = SchedulerInput.model_validate(raw).revised(
        locks=[
            _pin("resident-001", "outpatient_gyn", [21, 22]),
            _pin("resident-002", "outpatient_gyn", [21, 22]),
        ]
    )
    diagnostics = _locked_capacity_conflicts(_problem(instance))
    assert len(diagnostics) == 1
    (diagnostic,) = diagnostics
    assert diagnostic.code == "locked_capacity_conflict"
    assert "PGY1" in diagnostic.message
    assert "at most 1" in diagnostic.message
    assert diagnostic.resident_ids == ["resident-001", "resident-002"]
    assert diagnostic.weeks == [21, 22]


def _nonrepeatable_instance() -> SchedulerInput:
    instance = sample_instance()
    electives = instance.electives.model_copy(
        update={
            "rotation_options": [
                (
                    ElectiveRotationOption(
                        rotation_id=option.rotation_id,
                        eligible_pgys=[1],
                        eligible_block_sizes=[2],
                        repeatable=False,
                    )
                    if option.rotation_id == "geriatrics"
                    else option
                )
                for option in instance.electives.rotation_options
            ]
        }
    )
    return instance.revised(electives=electives)


def test_locked_nonrepeatable_elective_twice_is_named() -> None:
    instance = _nonrepeatable_instance().revised(
        locks=[
            _pin("resident-001", "geriatrics", [5, 6], elective=True),
            _pin("resident-001", "geriatrics", [15, 16], elective=True),
        ]
    )
    diagnostics = _locked_elective_repeats(_problem(instance))
    assert len(diagnostics) == 1
    (diagnostic,) = diagnostics
    assert diagnostic.code == "locked_elective_repeat"
    assert diagnostic.resident_ids == ["resident-001"]
    assert diagnostic.weeks == [5, 6, 15, 16]
    assert "Avery Chen" in diagnostic.message
    assert "only once as an elective" in diagnostic.message
    assert diagnostic.suggestions


def test_identical_duplicate_elective_locks_are_one_block() -> None:
    instance = _nonrepeatable_instance().revised(
        locks=[
            _pin("resident-001", "geriatrics", [5, 6], elective=True),
            _pin("resident-001", "geriatrics", [5, 6], elective=True),
        ]
    )
    assert _locked_elective_repeats(_problem(instance)) == []


def test_single_elective_lock_is_silent() -> None:
    instance = _nonrepeatable_instance().revised(
        locks=[_pin("resident-001", "geriatrics", [5, 6], elective=True)]
    )
    assert _locked_elective_repeats(_problem(instance)) == []


def test_repeatable_elective_locked_twice_is_silent() -> None:
    # Geriatrics stays repeatable in the stock sample catalog.
    instance = sample_instance().revised(
        locks=[
            _pin("resident-001", "geriatrics", [5, 6], elective=True),
            _pin("resident-001", "geriatrics", [15, 16], elective=True),
        ]
    )
    assert _locked_elective_repeats(_problem(instance)) == []
