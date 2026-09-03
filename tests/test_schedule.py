import pytest
from pydantic import ValidationError

from rbs.models.enums import RotationKind, SolverEngineName, SolverStatus
from rbs.models.schedule import Assignment, Schedule, ScheduleMeta
from rbs.runner import solve_instance
from rbs.solver.core import get_engine
from rbs.solver.core.decode import final_status_for


def test_stub_engine_empty_schedule(instance) -> None:
    schedule = get_engine("stub").solve(instance, options=instance.solver)
    assert schedule.meta.engine is SolverEngineName.STUB
    assert schedule.meta.status is SolverStatus.NOT_IMPLEMENTED
    assert schedule.assignments == []
    assert set(schedule.unassigned) == {resident.id for resident in instance.residents}


def test_cp_sat_engine_is_registered() -> None:
    engine = get_engine("cp_sat")
    assert engine.name is SolverEngineName.CP_SAT


def test_postprocessing_downgrades_only_final_optimal_status() -> None:
    assert (
        final_status_for(SolverStatus.OPTIMAL, postprocessed=True, valid=True)
        is SolverStatus.FEASIBLE
    )
    assert (
        final_status_for(SolverStatus.OPTIMAL, postprocessed=False, valid=True)
        is SolverStatus.OPTIMAL
    )
    assert (
        final_status_for(SolverStatus.FEASIBLE, postprocessed=True, valid=False)
        is SolverStatus.UNKNOWN
    )


def test_unknown_engine_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        get_engine("highs")


def test_output_schema_round_trip(instance) -> None:
    schedule = solve_instance(instance, engine="stub")
    restored = Schedule.model_validate(schedule.model_dump(mode="json"))
    assert restored.meta.academic_year == instance.academic_year
    assert restored.unassigned == schedule.unassigned


def test_solve_instance_forwards_the_existing_draft(instance, monkeypatch) -> None:
    from rbs import runner

    reference = get_engine("stub").solve(instance, options=instance.solver)
    captured = {}

    def recording_solve(solved_problem, *, options, reference_solution=None):
        captured["problem"] = solved_problem
        captured["options"] = options
        captured["reference"] = reference_solution
        return reference

    monkeypatch.setattr(runner, "solve_problem", recording_solve)

    result = runner.solve_instance(instance, reference_schedule=reference)

    assert result is reference
    from rbs.models.instance import SolverProblem

    assert captured == {
        "problem": SolverProblem.from_instance(instance),
        "options": instance.solver,
        "reference": reference,
    }


def test_stability_helpers_compare_resident_weeks_and_clinic_half_days(instance) -> None:
    from rbs.models.enums import Session, Weekday
    from rbs.models.schedule import AssignedClinic
    from rbs.solver.reference import (
        changed_resident_weeks,
        reference_clinic_half_days,
    )

    resident_id = instance.residents[0].id
    reference = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="fmed",
                start_week=1,
                end_week=2,
                weeks=[1, 2],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.MORNING,
                    )
                ],
            )
        ],
    )
    updated = reference.model_copy(
        update={
            "assignments": [
                reference.assignments[0].model_copy(
                    update={"rotation_id": "icu", "start_week": 2, "weeks": [2]}
                ),
                reference.assignments[0].model_copy(update={"end_week": 1, "weeks": [1]}),
            ]
        }
    )

    changed, compared = changed_resident_weeks(instance, reference, updated)

    assert (changed, compared) == (1, 2)
    assert reference_clinic_half_days(instance, reference) == {
        (resident_id, 1, Weekday.MONDAY, Session.MORNING),
        (resident_id, 2, Weekday.MONDAY, Session.MORNING),
    }


def test_assignment_range_must_be_internally_consistent() -> None:
    with pytest.raises(ValidationError, match="contiguous range"):
        Assignment(
            resident_id="resident",
            rotation_id="rotation",
            start_week=5,
            end_week=1,
            weeks=[99],
        )


def test_elective_assignments_must_be_marked_elective() -> None:
    assignment = Assignment(
        resident_id="resident",
        rotation_id="elective",
        kind=RotationKind.STANDARD,
        start_week=1,
        end_week=1,
        weeks=[1],
    )

    assert assignment.kind is RotationKind.STANDARD
    assert not assignment.elective


def test_elective_fallback_requires_an_elective_clinic_assignment(instance) -> None:
    fallback = Assignment(
        resident_id=instance.residents[0].id,
        rotation_id="clinic",
        kind=RotationKind.CLINIC,
        elective=True,
        elective_fallback=True,
        start_week=1,
        end_week=2,
        weeks=[1, 2],
    )

    assert fallback.elective_fallback
    assert instance.assignment_name("clinic", elective=True) == (
        "Clinic (Elective fallback)"
    )
    with pytest.raises(ValidationError, match="requires an Elective-marked Clinic"):
        Assignment(
            resident_id="resident",
            rotation_id="clinic",
            kind=RotationKind.CLINIC,
            elective_fallback=True,
            start_week=1,
            end_week=1,
            weeks=[1],
        )


def test_legacy_assignments_default_to_not_being_elective_fallback() -> None:
    assignment = Assignment.model_validate(
        {
            "resident_id": "resident",
            "rotation_id": "rotation",
            "start_week": 1,
            "end_week": 1,
            "weeks": [1],
        }
    )

    assert assignment.elective_fallback is False


def test_schedule_derives_grid_and_coverage_from_assignments(instance) -> None:
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
                rotation_id="fmed",
                start_week=1,
                end_week=2,
                weeks=[1, 2],
            )
        ],
    )
    assert schedule.week_grid == {resident_id: {"1": "fmed", "2": "fmed"}}
    assert [(row.week, row.rotation_id) for row in schedule.coverage] == [
        (1, "fmed"),
        (2, "fmed"),
    ]
    payload = schedule.model_dump(mode="json")
    assert "week_grid" not in payload
    assert "coverage" not in payload


def test_legacy_weekend_output_is_rejected() -> None:
    with pytest.raises(ValidationError, match="weekend_day"):
        Assignment.model_validate(
            {
                "resident_id": "resident",
                "rotation_id": "rotation",
                "start_week": 1,
                "end_week": 1,
                "weeks": [1],
                "weekend_day": "saturday",
            }
        )
