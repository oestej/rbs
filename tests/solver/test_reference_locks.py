"""Reference clinic locks that outlived the rotation configuration.

A locked clinic session carried over from a previous draft compiles to a hard
equality: locks are the operator's special-case mechanism and are never
skipped. When later edits change the rotation's clinic days, the lock is kept
as a one-off extra session with a warning. When time off, the academic
half-day, a vacation week, or a clinic-free rotation rules the session out
entirely, the lock stays enforced and the resulting infeasibility names the
conflicting rule instead of failing unexplained.

Pure-helper and compilation checks stay outside the ``solve`` marker group;
only the end-to-end honoring test starts a real CP-SAT search.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rbs.catalog import sample_instance
from rbs.models.enums import (
    Session,
    SolverEngineName,
    SolverStatus,
    Weekday,
)
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.locks import LockedPlacement
from rbs.models.schedule import (
    AssignedClinic,
    Assignment,
    Schedule,
    ScheduleMeta,
    SolverDiagnostic,
)
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.solver.core.base import empty_schedule
from rbs.solver.planning import Occurrence
from rbs.solver.reference import (
    HONORED_REFERENCE_LOCK,
    REFERENCE_LOCK_CONFLICT,
    reference_clinic_lock_barriers,
    reference_lock_conflict_diagnostic,
    viable_reference_clinic_locks,
)

RESIDENT = "resident-001"
FRIDAY_MORNING = (Weekday.FRIDAY, Session.MORNING)
FRIDAY_AFTERNOON = (Weekday.FRIDAY, Session.AFTERNOON)


def _exact_lock(rotation_id: str, weeks: list[int]) -> LockedPlacement:
    return LockedPlacement(
        resident_id=RESIDENT,
        rotation_id=rotation_id,
        weeks=weeks,
        exact_block=True,
    )


def _reference(
    instance_academic_year: str,
    rotation_id: str,
    weekday: Weekday,
    session: Session,
    week: int,
    *,
    start_week: int = 1,
) -> Schedule:
    weeks = list(range(start_week, start_week + 4))
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance_academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=RESIDENT,
                rotation_id=rotation_id,
                start_week=start_week,
                end_week=start_week + 3,
                weeks=weeks,
                clinic_slots=[
                    AssignedClinic(
                        weekday=weekday,
                        session=session,
                        week=week,
                        locked=True,
                    )
                ],
            )
        ],
    )


def _occurrence(rotation_id: str, duration_weeks: int = 4) -> dict[str, Occurrence]:
    key = f"{RESIDENT}:{rotation_id}:{duration_weeks}:0"
    return {
        key: Occurrence(
            key=key,
            resident_id=RESIDENT,
            pgy=1,
            rotation_id=rotation_id,
            duration_weeks=duration_weeks,
            group_id=key,
        )
    }


def _starts(occurrences: dict, start: int = 1) -> dict:
    return {key: (start,) for key in occurrences}


def _locked_instance(rotation_id: str):
    return sample_instance().revised(locks=[_exact_lock(rotation_id, [1, 2, 3, 4])])


def test_changed_rotation_day_is_viable_for_an_extra_session() -> None:
    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)
    occurrences = _occurrence("fmed")

    viable = viable_reference_clinic_locks(
        problem, reference, occurrences, _starts(occurrences)
    )

    assert viable == {(RESIDENT, 2, *FRIDAY_MORNING): True}


def test_clinic_free_rotation_lock_is_viable_for_an_extra_session() -> None:
    instance = sample_instance().revised(locks=[_exact_lock("icu", [5, 6, 7, 8])])
    problem = SolverProblem.from_instance(instance)
    reference = _reference(
        instance.academic_year, "icu", *FRIDAY_MORNING, 6, start_week=5
    )
    occurrences = _occurrence("icu")

    viable = viable_reference_clinic_locks(
        problem, reference, occurrences, _starts(occurrences, 5)
    )

    assert viable == {(RESIDENT, 6, *FRIDAY_MORNING): True}
    assert (
        reference_clinic_lock_barriers(
            problem, reference, occurrences, _starts(occurrences, 5)
        )
        == {}
    )


def test_away_rotation_lock_has_a_barrier() -> None:
    raw = sample_instance().model_dump(mode="json")
    for rotation in raw["rotations"]:
        if rotation["id"] == "fmed":
            rotation["away"] = True
    instance = SchedulerInput.model_validate(raw).revised(
        locks=[_exact_lock("fmed", [1, 2, 3, 4])]
    )
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)
    occurrences = _occurrence("fmed")

    barriers = reference_clinic_lock_barriers(
        problem, reference, occurrences, _starts(occurrences)
    )

    assert barriers == {
        (RESIDENT, 2, *FRIDAY_MORNING): (
            "locked Family Med Education Service blocks, which offer no clinic then"
        )
    }


def test_academic_half_day_lock_has_a_barrier() -> None:
    instance = sample_instance()
    problem = SolverProblem.from_instance(instance)
    reference = _reference(
        instance.academic_year, "fmed", Weekday.WEDNESDAY, Session.AFTERNOON, 2
    )

    barriers = reference_clinic_lock_barriers(problem, reference, {}, {})

    assert barriers == {
        (RESIDENT, 2, Weekday.WEDNESDAY, Session.AFTERNOON): "the academic half-day"
    }


def test_vacation_week_lock_has_a_barrier() -> None:
    instance = sample_instance()
    problem = SolverProblem.from_instance(instance)
    # resident-001 vacations in week 12 in the sample catalog.
    reference = _reference(
        instance.academic_year, "fmed", *FRIDAY_MORNING, 12, start_week=9
    )

    barriers = reference_clinic_lock_barriers(problem, reference, {}, {})

    assert barriers == {(RESIDENT, 12, *FRIDAY_MORNING): "a vacation week"}


def test_day_off_lock_has_a_barrier() -> None:
    instance = sample_instance()
    friday = instance.calendar.first_week_start + timedelta(days=7 + 4)
    residents = [
        resident.model_copy(update={"days_off": [friday]})
        if resident.id == RESIDENT
        else resident
        for resident in instance.residents
    ]
    instance = instance.revised(residents=residents)
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)

    barriers = reference_clinic_lock_barriers(problem, reference, {}, {})

    assert barriers == {(RESIDENT, 2, *FRIDAY_MORNING): "a day off"}


def test_special_rotation_lock_has_a_barrier() -> None:
    instance = sample_instance()
    friday = instance.calendar.first_week_start + timedelta(days=7 + 4)
    instance = instance.revised(
        special_rotations=[
            *instance.special_rotations,
            SpecialRotation(
                id="test-ise",
                name="Skills Exam",
                kind=SpecialRotationKind.EVENT,
                start_date=friday,
                end_date=friday,
                session=Session.MORNING,
                resident_ids=[RESIDENT],
            ),
        ]
    )
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)

    barriers = reference_clinic_lock_barriers(problem, reference, {}, {})

    assert barriers == {
        (RESIDENT, 2, *FRIDAY_MORNING): "the Skills Exam special rotation"
    }


def test_template_offered_lock_has_no_barrier() -> None:
    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_AFTERNOON, 2)
    occurrences = _occurrence("fmed")

    assert (
        reference_clinic_lock_barriers(
            problem, reference, occurrences, _starts(occurrences)
        )
        == {}
    )


def test_conflict_diagnostic_names_each_barrier() -> None:
    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)
    barriers = {(RESIDENT, 2, *FRIDAY_MORNING): "locked ICU blocks"}

    diagnostic = reference_lock_conflict_diagnostic(problem, reference, barriers)

    assert diagnostic.code == REFERENCE_LOCK_CONFLICT
    assert "Avery Chen" in diagnostic.message
    assert "week 2" in diagnostic.message
    assert "(blocked by locked ICU blocks)" in diagnostic.message
    assert diagnostic.resident_ids == [RESIDENT]
    assert diagnostic.weeks == [2]
    assert diagnostic.suggestions


def test_compile_records_a_synthetic_entry_for_the_viable_lock() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)

    compiled = compile_problem(
        problem, instance.solver, cp_model, reference_schedule=reference
    )

    assert compiled.clinic.synthetic_reference_locks == {
        (RESIDENT, 2, *FRIDAY_MORNING)
    }


def test_compile_records_a_synthetic_entry_on_a_clinic_free_rotation() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    instance = sample_instance().revised(locks=[_exact_lock("icu", [5, 6, 7, 8])])
    problem = SolverProblem.from_instance(instance)
    reference = _reference(
        instance.academic_year, "icu", *FRIDAY_MORNING, 6, start_week=5
    )

    compiled = compile_problem(
        problem, instance.solver, cp_model, reference_schedule=reference
    )

    assert compiled.clinic.synthetic_reference_locks == {
        (RESIDENT, 6, *FRIDAY_MORNING)
    }


def test_honored_session_on_a_clinic_free_rotation_decodes_with_a_site() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.clinic_allocation import assign_clinic_sites
    from rbs.solver.core.compile import compile_problem
    from rbs.solver.core.decode import _decode_clinic_slots

    instance = sample_instance().revised(locks=[_exact_lock("icu", [5, 6, 7, 8])])
    problem = SolverProblem.from_instance(instance)
    reference = _reference(
        instance.academic_year, "icu", *FRIDAY_MORNING, 6, start_week=5
    )
    compiled = compile_problem(
        problem, instance.solver, cp_model, reference_schedule=reference
    )
    occurrence = next(
        item
        for item in compiled.context.occurrences
        if item.resident_id == RESIDENT and item.rotation_id == "icu"
    )
    entries = compiled.clinic.in_clinic[(RESIDENT, 6)]
    friday = [
        literal
        for _keys, weekday, session, literal in entries
        if weekday is Weekday.FRIDAY and session is Session.MORNING
    ]
    assert len(friday) == 1

    class _PlacedSolver:
        def Value(self, literal):  # noqa: N802 - mirrors the solver API
            return literal is friday[0]

    slots = _decode_clinic_slots(
        occurrence,
        compiled.context.rotations["icu"],
        [5, 6, 7, 8],
        None,
        compiled,
        _PlacedSolver(),
        set(),
    )

    assert [
        (slot.weekday, slot.session, slot.week, slot.admin) for slot in slots
    ] == [(Weekday.FRIDAY, Session.MORNING, 6, False)]
    assignment = Assignment(
        resident_id=RESIDENT,
        rotation_id="icu",
        start_week=5,
        end_week=8,
        weeks=[5, 6, 7, 8],
        clinic_slots=slots,
    )
    assign_clinic_sites(instance, [assignment], reference_schedule=reference)
    (slot,) = assignment.clinic_slots
    assert slot.site in instance.clinic_policy.site_ids
    assert slot.locked is True


def test_compile_records_nothing_for_a_template_offered_lock() -> None:
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_AFTERNOON, 2)

    compiled = compile_problem(
        problem, instance.solver, cp_model, reference_schedule=reference
    )

    assert compiled.clinic.synthetic_reference_locks == set()


@pytest.mark.solve
def test_stale_lock_is_honored_with_a_warning() -> None:
    from rbs.solver.core import get_engine

    instance = _locked_instance("fmed")
    instance = instance.model_copy(
        update={
            "solver": instance.solver.model_copy(
                update={"time_limit_seconds": 30, "random_seed": 1}
            )
        }
    )
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)

    schedule = get_engine("cp_sat").solve(
        problem, options=instance.solver, reference_schedule=reference
    )

    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert schedule.meta.validation_errors == []
    honored = [
        diagnostic
        for diagnostic in schedule.meta.diagnostics
        if diagnostic.code == HONORED_REFERENCE_LOCK
    ]
    assert len(honored) == 1
    assert honored[0].resident_ids == [RESIDENT]
    assert honored[0].weeks == [2]
    friday = [
        slot
        for assignment in schedule.assignments
        if assignment.resident_id == RESIDENT and 2 in assignment.weeks
        for slot in assignment.clinic_slots
        if slot.week == 2
        and slot.weekday is Weekday.FRIDAY
        and slot.session is Session.MORNING
        and not slot.admin
    ]
    assert len(friday) == 1
    assert friday[0].locked is True


def test_reference_conflict_is_restored_when_probes_find_nothing(
    monkeypatch,
) -> None:
    from rbs.solver.core import cp_sat

    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)
    provisional = reference_lock_conflict_diagnostic(
        problem, reference, {(RESIDENT, 2, *FRIDAY_MORNING): "a vacation week"}
    )
    schedule = empty_schedule(
        problem,
        engine=SolverEngineName.CP_SAT,
        status=SolverStatus.INFEASIBLE,
        notes=["CP-SAT returned INFEASIBLE"],
        diagnostics=[provisional],
    )
    monkeypatch.setattr(cp_sat, "explain_infeasibility", lambda *args: [])

    result = cp_sat._with_infeasibility_diagnostics(
        problem, instance.solver, schedule
    )

    assert [diagnostic.code for diagnostic in result.meta.diagnostics] == [
        REFERENCE_LOCK_CONFLICT
    ]
    assert provisional.message in result.meta.notes


def test_reference_conflict_yields_to_a_conclusive_probe(monkeypatch) -> None:
    from rbs.solver.core import cp_sat

    instance = _locked_instance("fmed")
    problem = SolverProblem.from_instance(instance)
    reference = _reference(instance.academic_year, "fmed", *FRIDAY_MORNING, 2)
    provisional = reference_lock_conflict_diagnostic(
        problem, reference, {(RESIDENT, 2, *FRIDAY_MORNING): "a vacation week"}
    )
    conclusive = SolverDiagnostic(
        code="resident_curriculum_coverage",
        message="Avery Chen cannot tile the year.",
    )
    schedule = empty_schedule(
        problem,
        engine=SolverEngineName.CP_SAT,
        status=SolverStatus.INFEASIBLE,
        notes=["CP-SAT returned INFEASIBLE"],
        diagnostics=[provisional],
    )
    monkeypatch.setattr(cp_sat, "explain_infeasibility", lambda *args: [conclusive])

    result = cp_sat._with_infeasibility_diagnostics(
        problem, instance.solver, schedule
    )

    assert result.meta.diagnostics == [conclusive]
    assert provisional.message not in result.meta.notes
