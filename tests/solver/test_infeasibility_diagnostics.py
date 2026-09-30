"""Deterministic tests for shared lock-conflict infeasibility diagnostics.

These checks are pure Python over the validated instance: they never start a
CP-SAT search, so they stay outside the ``solve`` marker group.
"""

from rbs.catalog import sample_instance
from rbs.models.attending import AttendingClinicCoverage
from rbs.models.clinic import clinic_slot_date
from rbs.models.clinic_site import ClinicStaffingMode
from rbs.models.elective import ElectiveRotationOption
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.locks import LockedPlacement
from rbs.solver.clinic_requirements import CODE as UNCOVERABLE_CLINIC_SESSION
from rbs.solver.clinic_requirements import uncoverable_clinic_sessions
from rbs.solver.core.diagnostics import (
    _locked_capacity_conflicts,
    _locked_elective_repeats,
)
from rbs.solver.planning import expand_occurrences


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


def _attending_managed_instance(**updates) -> SchedulerInput:
    """Stock sample with Cedar on attending-managed staffing (no coverage)."""
    instance = sample_instance()
    sites = [
        site.model_copy(update={"staffing_mode": ClinicStaffingMode.ATTENDING_MANAGED})
        if site.id == "cedar"
        else site
        for site in instance.clinic_policy.sites
    ]
    policy = instance.clinic_policy.model_copy(update={"sites": sites})
    return instance.revised(clinic_policy=policy, **updates)


def _probe(instance: SchedulerInput):
    return uncoverable_clinic_sessions(_problem(instance), allow_boundary_spans=False)


def test_managed_site_nobody_can_precept_names_the_unplaceable_block() -> None:
    instance = _attending_managed_instance(attendings=[], attending_schedules=[])
    diagnostics = _probe(instance)

    assert len(diagnostics) == 1
    (diagnostic,) = diagnostics
    assert diagnostic.code == UNCOVERABLE_CLINIC_SESSION
    assert "Inpatient Peds Metro" in diagnostic.message
    assert "no attending is available to precept at Cedar" in diagnostic.message
    expected_residents = sorted(
        {
            occurrence.resident_id
            for occurrence in expand_occurrences(_problem(instance))
            if occurrence.rotation_id == "inpatient_peds_metro"
        }
    )
    assert diagnostic.resident_ids == expected_residents
    assert any("can precept at Cedar" in item for item in diagnostic.suggestions)
    assert any("capacity-managed" in item for item in diagnostic.suggestions)


def test_managed_site_with_attendings_who_could_precept_stays_silent() -> None:
    # The solve schedules attendings, so hand-entered precepting is optional.
    instance = _attending_managed_instance()
    assert instance.attendings
    assert not any(
        schedule.weeks for schedule in instance.attending_schedules
        if any(
            half_day.clinic_id == "cedar"
            for week in schedule.weeks
            for half_day in week.half_days
        )
    )
    assert _probe(instance) == []


def test_stock_sample_has_no_uncoverable_clinic_session() -> None:
    assert _probe(sample_instance()) == []


def test_managed_site_with_coverage_stays_silent() -> None:
    instance = _attending_managed_instance(attendings=[], attending_schedules=[])
    problem = _problem(instance)
    first = problem.calendar.first_week_start
    coverage = [
        AttendingClinicCoverage(
            clinic_id="cedar",
            date=clinic_slot_date(first, week, Weekday.FRIDAY),
            session=Session.AFTERNOON,
            attendings=2,
        )
        for week in range(1, problem.calendar.weeks + 1)
    ]
    covered = problem.model_copy(update={"attending_coverage": coverage})
    assert uncoverable_clinic_sessions(covered, allow_boundary_spans=False) == []


def test_smaller_pick_escapes_through_the_live_site() -> None:
    instance = sample_instance()
    metro = next(
        rotation for rotation in instance.rotations if rotation.id == "inpatient_peds_metro"
    )
    rule = metro.clinic.model_copy(update={"half_days_per_week": 1})
    rotations = [
        rotation.model_copy(update={"clinic": rule})
        if rotation.id == "inpatient_peds_metro"
        else rotation
        for rotation in instance.rotations
    ]
    managed = _attending_managed_instance()
    escaped = managed.revised(rotations=rotations)
    assert _probe(escaped) == []


def test_probe_floor_matches_the_compiler_formula() -> None:
    from rbs.solver.clinic_requirements import _occupancy_floor as probe_floor
    from rbs.solver.core.objective_slots import _occupancy_floor as compiler_floor

    for pick in range(0, 5):
        for domain_size in range(1, 7):
            for surviving in range(0, 7):
                for negated in (False, True):
                    assert probe_floor(surviving, pick, domain_size, negated) == (
                        compiler_floor(surviving, pick, domain_size, negated)
                    )


def _clinic_lock_reference(
    problem: SolverProblem,
    resident_ids: list[str],
    *,
    in_clinic: bool = True,
):
    """A previous schedule that locks one clinic session in every resident week."""
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    weeks = list(range(1, problem.calendar.weeks + 1))
    return Schedule(
        meta=ScheduleMeta(
            academic_year=problem.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident_id,
                rotation_id="inpatient_peds_metro",
                start_week=1,
                end_week=weeks[-1],
                weeks=weeks,
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.MORNING,
                        admin=not in_clinic,
                        locked=True,
                    )
                ],
            )
            for resident_id in resident_ids
        ],
    )


def test_readiness_does_not_block_a_resolve_that_reference_clinic_locks_can_rescue() -> None:
    # A locked clinic session in the schedule a re-solve starts from can add a
    # one-off session to its resident's week, so no week it covers is dead.
    from rbs.solver.readiness import check_solve_readiness

    problem = _problem(_attending_managed_instance(attendings=[], attending_schedules=[]))
    (diagnostic,) = uncoverable_clinic_sessions(problem, allow_boundary_spans=True)
    reference = _clinic_lock_reference(problem, diagnostic.resident_ids)

    assert not check_solve_readiness(problem).ready
    assert check_solve_readiness(problem, reference_schedule=reference).ready
    assert uncoverable_clinic_sessions(
        problem, allow_boundary_spans=False, reference_schedule=reference
    ) == []


def test_reference_admin_locks_do_not_silence_the_probe() -> None:
    # A locked Admin session only removes clinic time, so it cannot rescue a week.
    problem = _problem(_attending_managed_instance(attendings=[], attending_schedules=[]))
    (diagnostic,) = uncoverable_clinic_sessions(problem, allow_boundary_spans=True)
    reference = _clinic_lock_reference(problem, diagnostic.resident_ids, in_clinic=False)

    assert uncoverable_clinic_sessions(
        problem, allow_boundary_spans=True, reference_schedule=reference
    ) == [diagnostic]


def test_infeasibility_explanation_uses_the_probe_on_a_resolve() -> None:
    from rbs.solver.core.diagnostics import explain_infeasibility

    instance = _attending_managed_instance(attendings=[], attending_schedules=[])
    problem = _problem(instance)
    unrelated = _clinic_lock_reference(problem, [problem.residents[0].id], in_clinic=False)

    codes = {
        diagnostic.code
        for diagnostic in explain_infeasibility(problem, instance.solver, unrelated)
    }
    assert UNCOVERABLE_CLINIC_SESSION in codes
