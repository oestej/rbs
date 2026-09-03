from copy import deepcopy

import pytest

from rbs.catalog import sample_instance
from rbs.models.enums import SolverStatus
from rbs.models.instance import SchedulerInput
from rbs.solver.core import get_engine

# Every test here runs a real CP-SAT search.
pytestmark = pytest.mark.solve


def _rotation(rotation_id: str, *, kind: str = "standard") -> dict:
    return {
        "id": rotation_id,
        "code": rotation_id[:6].upper(),
        "name": rotation_id.title(),
        "kind": kind,
        "pgy_rules": [
            {
                "pgy": 1,
                "min_concurrent": None,
                "max_concurrent": None,
                "prerequisite_rotation_ids": [],
                "earliest_start_week": None,
                "block_configs": [
                    {
                        "duration_weeks": 1,
                        "vacation": {
                            "allowed": False,
                            "max_weeks_per_block": None,
                        },
                    }
                ],
            }
        ],
        "clinic": None,
        "capacity": {"min_concurrent": None, "max_concurrent": None},
        "away": False,
        "no_clinic_hours": True,
        "no_weekend_call": False,
        "max_consecutive_weeks": 6,
        "max_total_weeks": None,
    }


def _matching_problem(
    preferences: list[tuple[str, int]],
    *,
    locks: list[dict] | None = None,
    clinic_max: int | None = None,
    a_repeatable: bool = True,
) -> SchedulerInput:
    raw = sample_instance().model_dump(mode="json")
    resident = deepcopy(raw["residents"][0])
    resident.update(
        {
            "vacation_weeks": [],
            "days_off": [],
            "clinic_half_days": [],
            "elective_preferences": [
                {"rotation_id": rotation_id, "duration_weeks": duration}
                for rotation_id, duration in preferences
            ],
        }
    )
    raw["residents"] = [resident]
    raw["academic_half_day_overrides"] = []
    raw["locks"] = locks or []
    raw["manual_clinic_blocks"] = []
    raw["resident_rotation_overrides"] = []
    raw["special_rotations"] = []
    raw["rotation_groups"] = []
    raw["rotations"] = [
        _rotation("elective", kind="elective"),
        _rotation("a"),
        _rotation("b"),
        _rotation("clinic", kind="clinic"),
        _rotation("x"),
        _rotation("y"),
    ]
    clinic = next(rotation for rotation in raw["rotations"] if rotation["id"] == "clinic")
    clinic["capacity"]["max_concurrent"] = clinic_max
    raw["requirements"] = [
        {
            "pgy": 1,
            "blocks": [
                {"rotation_id": "elective", "duration_weeks": 1, "count": 2},
                {"rotation_id": "x", "duration_weeks": 1, "count": 25},
                {"rotation_id": "y", "duration_weeks": 1, "count": 25},
            ],
        }
    ]
    raw["electives"] = {
        "color": raw["electives"]["color"],
        "rotation_options": [
            {
                "rotation_id": "a",
                "eligible_pgys": [1],
                "eligible_block_sizes": [1],
                "repeatable": a_repeatable,
            },
            {
                "rotation_id": "b",
                "eligible_pgys": [1],
                "eligible_block_sizes": [1],
                "repeatable": True,
            },
        ],
    }
    raw["solver"].update(
        {
            "time_limit_seconds": 5,
            "solve_attempts": 1,
            "num_workers": 2,
            "auto_balance_clinic_blocks": False,
            "relative_gap": 0,
        }
    )
    return SchedulerInput.model_validate(raw)


def _solve(instance: SchedulerInput):
    return get_engine("cp_sat").solve(instance, options=instance.solver)


def _rank_conflict_problem() -> SchedulerInput:
    raw = _matching_problem([]).model_dump(mode="json")
    residents = []
    for index in range(2):
        resident = deepcopy(raw["residents"][0])
        resident["id"] = f"resident-{index + 1:03d}"
        resident["name"] = f"Resident {index + 1}"
        resident["elective_preferences"] = [
            {"rotation_id": "a", "duration_weeks": 1},
            {"rotation_id": "b", "duration_weeks": 1},
        ]
        residents.append(resident)
    raw["residents"] = residents
    raw["requirements"][0]["blocks"] = [
        {"rotation_id": "elective", "duration_weeks": 1, "count": 1},
        {"rotation_id": "x", "duration_weeks": 1, "count": 26},
        {"rotation_id": "y", "duration_weeks": 1, "count": 25},
    ]
    next(rotation for rotation in raw["rotations"] if rotation["id"] == "a")["capacity"][
        "max_concurrent"
    ] = 1
    raw["locks"] = [
        {
            "resident_id": resident["id"],
            "rotation_id": "x" if week % 2 == 0 else "y",
            "weeks": [week],
            "exact_block": True,
        }
        for resident in residents
        for week in range(2, 53)
    ]
    return SchedulerInput.model_validate(raw)


def test_empty_stack_uses_clinic_for_every_direct_elective_block() -> None:
    schedule = _solve(_matching_problem([]))

    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    electives = [assignment for assignment in schedule.assignments if assignment.elective]
    assert len(electives) == 2
    assert all(assignment.rotation_id == "clinic" for assignment in electives)
    assert all(assignment.elective_fallback for assignment in electives)
    assert schedule.meta.metrics.elective_fallback_blocks == 2


def test_one_use_request_excludes_unranked_services_and_then_falls_back() -> None:
    schedule = _solve(_matching_problem([("a", 1)]))

    electives = [assignment for assignment in schedule.assignments if assignment.elective]
    assert sorted(assignment.rotation_id for assignment in electives) == ["a", "clinic"]
    assert not any(assignment.rotation_id == "b" for assignment in electives)
    assert schedule.meta.metrics.elective_fallback_blocks == 1
    assert schedule.meta.metrics.elective_preference_rank_counts == [1]


def test_duplicate_requests_can_match_the_same_service_more_than_once() -> None:
    schedule = _solve(_matching_problem([("a", 1), ("a", 1)]))

    electives = [assignment for assignment in schedule.assignments if assignment.elective]
    assert [assignment.rotation_id for assignment in electives].count("a") == 2
    assert schedule.meta.metrics.elective_fallback_blocks == 0
    assert schedule.meta.metrics.elective_preference_rank_counts == [1, 1]


def test_nonrepeatable_service_cannot_satisfy_two_exact_elective_locks() -> None:
    schedule = _solve(
        _matching_problem(
            [("a", 1), ("a", 1)],
            a_repeatable=False,
            locks=[
                {
                    "resident_id": "resident-001",
                    "rotation_id": "a",
                    "weeks": [week],
                    "elective": True,
                    "exact_block": True,
                }
                for week in (1, 2)
            ],
        )
    )

    assert schedule.meta.status is SolverStatus.INFEASIBLE


def test_global_rank_maximal_matching_uses_second_choice_before_fallback() -> None:
    schedule = _solve(_rank_conflict_problem())

    electives = [assignment for assignment in schedule.assignments if assignment.elective]
    assert sorted(assignment.rotation_id for assignment in electives) == ["a", "b"]
    assert schedule.meta.metrics.elective_fallback_blocks == 0
    assert schedule.meta.metrics.elective_preference_rank_counts == [1, 1]


def test_unranked_elective_lock_wins_without_opening_that_service_elsewhere() -> None:
    schedule = _solve(
        _matching_problem(
            [("a", 1)],
            locks=[
                {
                    "resident_id": "resident-001",
                    "rotation_id": "b",
                    "weeks": [1],
                    "elective": True,
                    "exact_block": True,
                }
            ],
        )
    )

    electives = [assignment for assignment in schedule.assignments if assignment.elective]
    locked = next(assignment for assignment in electives if assignment.rotation_id == "b")
    assert locked.weeks == [1]
    assert [assignment.rotation_id for assignment in electives].count("b") == 1
    assert schedule.meta.metrics.elective_fallback_blocks == 0


def test_incompatible_clinic_capacity_reports_no_fallback_solution() -> None:
    schedule = _solve(_matching_problem([], clinic_max=0))

    assert schedule.is_empty()
    assert schedule.meta.status is SolverStatus.INFEASIBLE
