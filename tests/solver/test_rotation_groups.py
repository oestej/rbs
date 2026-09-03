from __future__ import annotations

from copy import deepcopy

import pytest
from ortools.sat.python import cp_model

from rbs.catalog import sample_instance
from rbs.models.instance import SchedulerInput
from rbs.solver.core.compile import compile_problem

# Every test here runs a real CP-SAT search.
pytestmark = pytest.mark.solve


def _rotation(
    rotation_id: str,
    duration_weeks: int,
    *,
    prerequisites: list[str] | None = None,
) -> dict:
    return {
        "id": rotation_id,
        "code": rotation_id.upper(),
        "name": rotation_id.upper(),
        "kind": "standard",
        "pgy_rules": [
            {
                "pgy": 1,
                "min_concurrent": None,
                "max_concurrent": None,
                "prerequisite_rotation_ids": prerequisites or [],
                "earliest_start_week": None,
                "block_configs": [
                    {
                        "duration_weeks": duration_weeks,
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
        "max_consecutive_weeks": max(duration_weeks, 6),
        "max_total_weeks": None,
    }


def _group_problem(
    members: list[tuple[str, int]],
    *,
    prerequisites: dict[str, list[str]] | None = None,
    locks: list[dict] | None = None,
) -> SchedulerInput:
    raw = sample_instance().model_dump(mode="json")
    resident = deepcopy(raw["residents"][0])
    resident["vacation_weeks"] = []
    resident["days_off"] = []
    resident["clinic_half_days"] = []
    raw["residents"] = [resident]
    raw["academic_half_day_overrides"] = []
    raw["locks"] = locks or []
    raw["manual_clinic_blocks"] = []
    raw["resident_rotation_overrides"] = []
    raw["special_rotations"] = []

    group_weeks = sum(duration for _rotation_id, duration in members)
    filler_weeks = 52 - group_weeks
    assert filler_weeks % 2 == 0
    filler_count = filler_weeks // 2
    raw["rotations"] = [
        *[
            _rotation(
                rotation_id,
                duration,
                prerequisites=(prerequisites or {}).get(rotation_id),
            )
            for rotation_id, duration in members
        ],
        _rotation("x", 1),
        _rotation("y", 1),
    ]
    raw["requirements"] = [
        {
            "pgy": 1,
            "blocks": [
                *[
                    {
                        "rotation_id": rotation_id,
                        "duration_weeks": duration,
                        "count": 1,
                    }
                    for rotation_id, duration in members
                ],
                {"rotation_id": "x", "duration_weeks": 1, "count": filler_count},
                {"rotation_id": "y", "duration_weeks": 1, "count": filler_count},
            ],
        }
    ]
    raw["rotation_groups"] = [
        {"pgy": 1, "rotation_ids": [rotation_id for rotation_id, _duration in members]}
    ]
    raw["electives"] = {
        "color": raw["electives"]["color"],
        "rotation_options": [],
    }
    raw["solver"].update(
        {
            "time_limit_seconds": 5,
            "solve_attempts": 1,
            "auto_balance_clinic_blocks": False,
        }
    )
    return SchedulerInput.model_validate(raw)


def _solve_status(instance: SchedulerInput):
    problem = compile_problem(instance, instance.solver, cp_model)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    return solver.Solve(problem.context.model)


def test_group_members_support_mixed_durations_and_any_order() -> None:
    instance = _group_problem(
        [("a", 1), ("b", 2), ("c", 3)],
        prerequisites={"a": ["c"]},
        locks=[
            {
                "resident_id": "resident-001",
                "rotation_id": "c",
                "weeks": [1, 2, 3],
                "exact_block": True,
            },
            {
                "resident_id": "resident-001",
                "rotation_id": "a",
                "weeks": [4],
                "exact_block": True,
            },
            {
                "resident_id": "resident-001",
                "rotation_id": "b",
                "weeks": [5, 6],
                "exact_block": True,
            },
        ],
    )

    assert _solve_status(instance) == cp_model.OPTIMAL


def test_group_prerequisites_control_direction_independently() -> None:
    instance = _group_problem(
        [("a", 2), ("b", 2)],
        prerequisites={"b": ["a"]},
        locks=[
            {
                "resident_id": "resident-001",
                "rotation_id": "b",
                "weeks": [1, 2],
                "exact_block": True,
            },
            {
                "resident_id": "resident-001",
                "rotation_id": "a",
                "weeks": [3, 4],
                "exact_block": True,
            },
        ],
    )

    assert _solve_status(instance) == cp_model.INFEASIBLE


def test_manual_exact_block_can_release_one_group_instance() -> None:
    locks = [
        {
            "resident_id": "resident-001",
            "rotation_id": "a",
            "weeks": [1, 2],
            "exact_block": True,
        },
        {
            "resident_id": "resident-001",
            "rotation_id": "b",
            "weeks": [5, 6],
            "exact_block": True,
        },
    ]
    grouped = _group_problem([("a", 2), ("b", 2)], locks=locks)
    exempt_locks = deepcopy(locks)
    exempt_locks[0]["grouping_exempt"] = True
    exempt = _group_problem([("a", 2), ("b", 2)], locks=exempt_locks)

    assert _solve_status(grouped) == cp_model.INFEASIBLE
    assert _solve_status(exempt) in {cp_model.OPTIMAL, cp_model.FEASIBLE}
