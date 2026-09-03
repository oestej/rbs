from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest

from rbs.catalog import sample_instance as unconfigured_sample_instance
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.resident import ResidentClinicHalfDay
from rbs.models.rotation import ClinicSlot
from rbs.solver.core import get_engine
from rbs.solver.core.compile import compile_problem
from rbs.solver.core.context import PlanningContext, new_clinic_decision
from rbs.solver.core.kinds import fmed as fmed_kind
from rbs.solver.core.objective import _preferred_slot_penalties
from rbs.solver.planning import Occurrence, expand_occurrences, legal_starts, rotate_domain
from rbs.ui.clinic.board import (
    attending_load,
    clinic_headcount,
    is_academic,
    occupancy,
    occupant_site,
    weekly_attending_sessions,
)

ENGINE_DIR = Path(__file__).resolve().parents[2] / "src" / "rbs" / "solver" / "core"


def sample_instance() -> SchedulerInput:
    """Return the sample year with one explicitly configured test elective."""
    instance = unconfigured_sample_instance()
    raw = instance.model_dump(mode="json")
    source = next(
        rotation for rotation in raw["rotations"] if rotation["id"] == "elective"
    )
    configured = {
        **source,
        "id": "configured_test_elective",
        "code": "T-ELEC",
        "name": "Configured Test Elective",
    }
    raw["rotations"].append(configured)
    raw["electives"]["rotation_options"] = [
        {
            "rotation_id": configured["id"],
            "eligible_block_sizes": [2],
        }
    ]
    return SchedulerInput.model_validate(raw)


def _total_week_limit_instance(
    max_total_weeks: int | None,
    *,
    pgy_max_total_weeks: int | None = None,
) -> SchedulerInput:
    """Build a one-resident Elective-only year for focused CP-SAT limit tests."""
    raw = unconfigured_sample_instance().model_dump(mode="json")
    resident = raw["residents"][0]
    resident["vacation_weeks"] = []
    resident["days_off"] = []
    resident["clinic_half_days"] = []
    raw["residents"] = [resident]
    raw["academic_half_day_overrides"] = []
    raw["locks"] = []
    raw["manual_clinic_blocks"] = []
    raw["resident_rotation_overrides"] = []
    raw["special_rotations"] = []
    raw["rotation_groups"] = []

    source = next(
        rotation for rotation in raw["rotations"] if rotation["id"] == "elective"
    )
    source["clinic"] = None
    source["no_clinic_hours"] = True
    source["max_consecutive_weeks"] = 6
    source["pgy_rules"] = [
        rule for rule in source["pgy_rules"] if rule["pgy"] == 1
    ]
    limited = {
        **source,
        "id": "limited_elective",
        "code": "LIMIT",
        "name": "Limited Elective",
        "max_total_weeks": max_total_weeks,
        "pgy_rules": [
            {**rule, "max_total_weeks": pgy_max_total_weeks}
            for rule in source["pgy_rules"]
        ],
    }
    fallback = {
        **source,
        "id": "fallback_elective",
        "code": "FALLBK",
        "name": "Fallback Elective",
        "max_total_weeks": None,
    }
    clinic_fallback = {
        **source,
        "id": "clinic",
        "code": "CLINIC",
        "name": "Clinic",
        "kind": "clinic",
        "max_total_weeks": None,
    }
    raw["rotations"] = [source, limited, fallback, clinic_fallback]
    raw["requirements"] = [
        {
            "pgy": 1,
            "blocks": [
                {
                    "rotation_id": "elective",
                    "duration_weeks": 2,
                    "count": 26,
                }
            ],
        }
    ]
    raw["electives"]["rotation_options"] = [
        {"rotation_id": "limited_elective", "eligible_block_sizes": [2], "repeatable": True},
        {"rotation_id": "fallback_elective", "eligible_block_sizes": [2], "repeatable": True},
    ]
    raw["residents"][0]["elective_preferences"] = [
        *[
            {"rotation_id": "limited_elective", "duration_weeks": 2}
            for _ in range(26)
        ],
        *[
            {"rotation_id": "fallback_elective", "duration_weeks": 2}
            for _ in range(26)
        ],
    ]
    return SchedulerInput.model_validate(raw)


def _secondary_site_id(instance) -> str:
    return next(
        site_id
        for site_id in instance.clinic_policy.site_ids
        if site_id != instance.clinic_policy.primary_site_id
    )


@pytest.fixture(scope="module")
def draft() -> tuple:
    instance = sample_instance()
    solver = instance.solver.model_copy(update={"time_limit_seconds": 30, "random_seed": 1})
    instance = instance.model_copy(update={"solver": solver})
    schedule = get_engine("cp_sat").solve(instance, options=instance.solver)
    # CP-SAT is an anytime solver: a budget miss yields an empty schedule and
    # every property assertion below then fails for the same uninformative
    # reason. Fail once, here, with the cause named instead.
    if schedule.is_empty():
        pytest.fail(
            f"draft solve found no feasible schedule within "
            f"{solver.time_limit_seconds:g}s (solver_status="
            f"{schedule.meta.solver_status}); the budget is too tight to test against"
        )
    return instance, schedule


def test_rotate_domain_permutes_stably() -> None:
    domain = ["mon-am", "tue-am", "wed-am", "thu-am"]
    assert rotate_domain(domain, "alpha") == rotate_domain(domain, "alpha")
    rotated = rotate_domain(domain, "resident-001:elective:2:0")
    assert sorted(rotated) == sorted(domain)
    assert rotate_domain([], "x") == []


def test_four_week_boundary_spans_are_opt_in() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    occurrence = Occurrence(
        key="boundary-test",
        resident_id=resident.id,
        pgy=resident.pgy,
        rotation_id="clinic",
        duration_weeks=2,
        group_id="boundary-test",
    )
    rotation = instance.rotation("clinic")

    strict = legal_starts(
        occurrence,
        resident,
        rotation,
        instance.calendar,
        vacation_weeks=set(),
    )
    permissive = legal_starts(
        occurrence,
        resident,
        rotation,
        instance.calendar,
        vacation_weeks=set(),
        allow_blocks_to_span_four_week_boundaries=True,
    )

    assert instance.solver.allow_blocks_to_span_four_week_boundaries is False
    assert 3 in strict
    assert 4 not in strict
    assert 4 in permissive


def test_infeasible_vacation_coverage_reports_actions() -> None:
    from rbs.models.special import SpecialRotation, SpecialRotationKind

    instance = _total_week_limit_instance(None)
    resident = instance.residents[0].model_copy(update={"vacation_weeks": [1]})
    conference_day = instance.calendar.first_week_start + timedelta(weeks=1)
    conference = SpecialRotation(
        id="special-diagnostic",
        name="Boundary Conference",
        kind=SpecialRotationKind.CONFERENCE,
        start_date=conference_day,
        end_date=conference_day,
        resident_ids=[resident.id],
    )
    instance = instance.revised(
        residents=[resident],
        special_rotations=[conference],
    )
    options = instance.solver.model_copy(
        update={"time_limit_seconds": 5, "solve_attempts": 1}
    )

    schedule = get_engine("cp_sat").solve(instance, options=options)

    assert schedule.meta.status is SolverStatus.INFEASIBLE
    assert len(schedule.meta.diagnostics) == 1
    diagnostic = schedule.meta.diagnostics[0]
    assert diagnostic.code == "resident_vacation_coverage"
    assert diagnostic.resident_ids == [resident.id]
    assert diagnostic.special_rotation_ids == [conference.id]
    assert diagnostic.weeks == [1, 2]
    assert resident.name in diagnostic.message
    assert resident.id not in diagnostic.message
    assert "vacation-like weeks 1–2" in diagnostic.message
    assert "Boundary Conference adds week 2" in diagnostic.message
    assert any("dates or assigned residents" in item for item in diagnostic.suggestions)


def test_preferred_clinic_slot_is_a_soft_objective_choice() -> None:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    preferred = ClinicSlot(
        weekday=Weekday.TUESDAY,
        session=Session.MORNING,
        preferred=True,
    )
    fallback = ClinicSlot(
        weekday=Weekday.THURSDAY,
        session=Session.AFTERNOON,
    )
    decision = new_clinic_decision(
        model,
        "preferred-clinic-test",
        [fallback, preferred],
        pick=1,
    )
    penalties, upper_bound = _preferred_slot_penalties({"test": decision})
    model.Minimize(sum(penalties))

    solver = cp_model.CpSolver()
    result = solver.Solve(model)

    assert result == cp_model.OPTIMAL
    assert upper_bound == 1
    assert decision.selected_slots(solver) == [preferred]


def test_pgy1_icu_occurrences_cannot_cover_week_one() -> None:
    instance = sample_instance()
    residents = {resident.id: resident for resident in instance.residents}
    rotation = instance.rotation("icu")
    icu = [occ for occ in expand_occurrences(instance) if occ.rotation_id == "icu" and occ.pgy == 1]
    assert icu
    for occ in icu:
        assert occ.prerequisite_rotation_ids == ("fmed", "emergency_medicine")
        assert occ.earliest_start_week == 5
        legal = legal_starts(occ, residents[occ.resident_id], rotation, instance.calendar)
        assert 1 not in legal
        assert min(legal) >= 2


def test_pgy1_emergency_follows_fmed() -> None:
    instance = sample_instance()
    occs = [
        occ
        for occ in expand_occurrences(instance)
        if occ.rotation_id == "emergency_medicine" and occ.pgy == 1
    ]
    assert occs
    for occ in occs:
        assert occ.prerequisite_rotation_ids == ("fmed",)


def test_manual_clinic_block_replaces_elective_at_fixed_start() -> None:
    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    raw["manual_clinic_blocks"] = [
        {
            "resident_id": "resident-001",
            "rotation_id": "clinic",
            "start_week": 1,
            "duration_weeks": 2,
            "replaces_rotation_id": "elective",
        }
    ]
    instance = type(instance).model_validate(raw)
    resident_occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == "resident-001"
    ]
    manual = next(
        occurrence
        for occurrence in resident_occurrences
        if occurrence.fixed_start_week is not None
    )

    assert manual.rotation_id == "clinic"
    assert manual.duration_weeks == 2
    assert manual.fixed_start_week == 1
    assert not any(
        occurrence.rotation_id == "elective"
        and occurrence.group_id == occurrence.key
        for occurrence in resident_occurrences
    )
    resident = next(
        item for item in instance.residents if item.id == manual.resident_id
    )
    assert legal_starts(
        manual,
        resident,
        instance.rotation("clinic"),
        instance.calendar,
    ) == [1]


def test_resident_mandatory_override_adds_placeable_block_and_consumes_elective() -> None:
    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    raw["resident_rotation_overrides"] = [
        {
            "resident_id": "resident-001",
            "rotation_id": "night_float",
            "duration_weeks": 2,
            "replaces_rotation_id": "elective",
        }
    ]
    instance = type(instance).model_validate(raw)

    occurrences = [
        occurrence
        for occurrence in expand_occurrences(instance)
        if occurrence.resident_id == "resident-001"
    ]

    assert sum(
        occurrence.rotation_id == "night_float" for occurrence in occurrences
    ) == 2
    assert not any(
        occurrence.rotation_id == "elective"
        and occurrence.group_id == occurrence.key
        for occurrence in occurrences
    )
def test_engine_does_not_hardcode_rotation_ids() -> None:
    banned = ("gyn_ob", "night_float", "icu", "emergency_medicine")
    for path in ENGINE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert f'"{token}"' not in text, f"{path.name} hardcodes {token!r}"
            assert f"'{token}'" not in text, f"{path.name} hardcodes {token!r}"


def test_individual_day_off_is_removed_from_clinic_model() -> None:
    from ortools.sat.python import cp_model

    instance = sample_instance()
    resident = instance.residents[0]
    tuesday_off = instance.calendar.first_week_start + timedelta(weeks=10, days=1)
    residents = [
        resident.model_copy(update={"days_off": [tuesday_off]}) if item.id == resident.id else item
        for item in instance.residents
    ]
    instance = instance.model_copy(update={"residents": residents})

    problem = compile_problem(instance, instance.solver, cp_model)
    entries = problem.clinic.in_clinic.get((resident.id, 11), [])

    assert entries
    assert not any(weekday is Weekday.TUESDAY for _keys, weekday, _session, _lit in entries)
    assert any(weekday is Weekday.MONDAY for _keys, weekday, _session, _lit in entries)


def test_academic_override_moves_clinic_model_exclusion_for_that_week() -> None:
    from ortools.sat.python import cp_model

    instance = sample_instance()
    raw = instance.model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {
            "week": 11,
            "weekday": Weekday.TUESDAY.value,
            "session": Session.MORNING.value,
        }
    ]
    instance = type(instance).model_validate(raw)

    problem = compile_problem(instance, instance.solver, cp_model)
    week_entries = [
        entry
        for (_resident_id, week), entries in problem.clinic.in_clinic.items()
        if week == 11
        for entry in entries
    ]

    assert week_entries
    assert not any(
        weekday is Weekday.TUESDAY and session is Session.MORNING
        for _keys, weekday, session, _lit in week_entries
    )
    assert any(
        weekday is Weekday.WEDNESDAY and session is Session.AFTERNOON
        for _keys, weekday, session, _lit in week_entries
    )


def test_reference_schedule_adds_stability_to_the_objective() -> None:
    from ortools.sat.python import cp_model

    from rbs.models.enums import SolverEngineName
    from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta

    instance = sample_instance().revised(lock_through_today=True)
    resident = instance.residents[0]
    reference = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="fmed",
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        site=instance.clinic_policy.primary_site_id,
                        week=1,
                    )
                ],
            )
        ],
    )

    problem = compile_problem(
        instance,
        instance.solver,
        cp_model,
        reference_schedule=reference,
    )

    assert problem.reference_schedule is reference
    assert problem.clinic.has_objective
    assert problem.clinic.stability_comparisons > 1


def test_cp_sat_draft_covers_the_year(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert schedule.unassigned == []
    assert not schedule.is_empty()

    weeks = {str(week) for week in range(1, 53)}
    for resident in instance.residents:
        grid = schedule.week_grid[resident.id]
        assert set(grid) == weeks


def test_cp_sat_resolve_preserves_the_existing_draft(draft) -> None:
    from rbs.solver.reference import (
        changed_resident_weeks,
        reference_clinic_half_days,
        reference_clinic_sites,
    )

    instance, reference = draft
    tuned = instance.revised(
        solver=instance.solver.model_copy(
            update={
                "time_limit_seconds": 10,
                "solve_attempts": 1,
                "random_seed": 1,
            }
        )
    )

    schedule = get_engine("cp_sat").solve(
        tuned,
        options=tuned.solver,
        reference_schedule=reference,
    )

    # Stability is a soft objective under a wall-clock budget, not a hard
    # constraint. CP-SAT is an anytime solver: it stops when the clock runs out,
    # never proving optimality, so an equally good solution that nudges a few
    # placements is a legitimate answer - and the less CPU the machine can spare,
    # the less the search converges. Demanding *exact* preservation made this
    # test fail about half the time, on both this branch and main, for a single
    # moved half-day out of ~1700. What is worth asserting is that re-solving
    # does not scramble the draft.
    assert not schedule.is_empty()
    changed_weeks, compared_weeks = changed_resident_weeks(tuned, reference, schedule)
    assert compared_weeks == 1248
    _assert_preserved("resident-week placements", changed_weeks, compared_weeks, BLOCK_STABILITY)
    assert any("1248 existing resident-week placements" in note for note in schedule.meta.notes)

    _assert_set_preserved(
        "half-days",
        reference_clinic_half_days(tuned, reference),
        reference_clinic_half_days(tuned, schedule),
    )
    _assert_set_preserved(
        "sites",
        reference_clinic_sites(tuned, reference),
        reference_clinic_sites(tuned, schedule),
    )


#: Below these the re-solve has scrambled the draft rather than nudged it.
#:
#: These are deliberately loose. The draft this is measured against is itself a
#: fresh anytime solve, so it is sometimes a mediocre schedule - and re-solving
#: then legitimately finds a better one and moves a good deal. That is the
#: solver working, not a stability regression, so the bound has to sit below it.
#: What these still catch is the failure that matters: a stability objective
#: that has stopped working at all, which moves most placements rather than a
#: few percent of them.
BLOCK_STABILITY = 0.90
CLINIC_STABILITY = 0.90


def _assert_preserved(what: str, moved: int, total: int, floor: float) -> None:
    """Fail with the size of the drift, not an unreadable diff."""
    preserved = 1 - moved / max(total, 1)
    assert preserved >= floor, (
        f"re-solving moved {moved} of {total} {what} "
        f"({preserved:.1%} preserved, expected at least {floor:.0%}). "
        "Drift this large usually means the solver had too little CPU to "
        "converge rather than a stability regression."
    )


def _assert_set_preserved(what: str, before, after) -> None:
    if isinstance(before, dict):
        keys = set(before) | set(after)
        moved = sum(1 for key in keys if before.get(key) != after.get(key))
        total = len(keys)
    else:
        moved = len(set(before) ^ set(after))
        total = max(len(set(before)), 1)
    _assert_preserved(f"clinic {what}", moved, total, CLINIC_STABILITY)


def test_cp_sat_honors_locks(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    for lock in instance.locks:
        for week in lock.weeks:
            assert schedule.week_grid[lock.resident_id][str(week)] == lock.rotation_id


def test_cp_sat_omits_clinic_for_no_clinic_rotations(draft) -> None:
    instance, schedule = draft
    rotations = {rotation.id: rotation for rotation in instance.rotations}

    for assignment in schedule.assignments:
        if rotations[assignment.rotation_id].clinic_hours_disabled:
            assert assignment.clinic_slots == []


def test_cp_sat_pins_inpatient_peds_metro_clinic_time_and_site(draft) -> None:
    instance, schedule = draft
    metro_assignments = [
        assignment
        for assignment in schedule.assignments
        if assignment.rotation_id == "inpatient_peds_metro"
    ]

    assert metro_assignments
    residents = instance.residents_by_id
    for assignment in metro_assignments:
        vacation = set(residents[assignment.resident_id].vacation_weeks)
        for week in assignment.weeks:
            slots = {
                (slot.weekday, slot.session, slot.site)
                for slot in assignment.clinic_slots
                if slot.week == week
            }
            if week in vacation:
                assert slots == set()
                continue
            assert slots == {
                (Weekday.FRIDAY, Session.MORNING, "maple"),
                (Weekday.FRIDAY, Session.AFTERNOON, "cedar"),
            }


def test_resident_half_day_is_modeled_on_non_away_rotations_only() -> None:
    from ortools.sat.python import cp_model

    instance = sample_instance()
    resident = instance.residents[0].model_copy(
        update={
            "clinic_half_days": [
                ResidentClinicHalfDay(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    sites=[instance.clinic_policy.primary_site_id],
                )
            ]
        }
    )
    residents = [
        resident if item.id == resident.id else item
        for item in instance.residents
    ]
    instance = SchedulerInput.model_validate(
        instance.model_copy(update={"residents": residents}).model_dump(mode="json")
    )
    problem = compile_problem(instance, instance.solver, cp_model)
    icu_keys = {
        occurrence.key
        for occurrence in problem.context.occurrences
        if occurrence.resident_id == resident.id and occurrence.rotation_id == "icu"
    }
    assert icu_keys
    assert any(
        not icu_keys.isdisjoint(keys)
        and weekday is Weekday.TUESDAY
        and session is Session.MORNING
        for entries in problem.clinic.in_clinic.values()
        for keys, weekday, session, _lit in entries
    )

    away_icu = instance.rotation("icu").model_copy(update={"away": True})
    rotations = [
        away_icu if rotation.id == away_icu.id else rotation
        for rotation in instance.rotations
    ]
    away_instance = instance.revised(rotations=rotations)
    away_problem = compile_problem(away_instance, away_instance.solver, cp_model)
    away_icu_keys = {
        occurrence.key
        for occurrence in away_problem.context.occurrences
        if occurrence.resident_id == resident.id and occurrence.rotation_id == "icu"
    }
    assert not any(
        not away_icu_keys.isdisjoint(keys)
        for entries in away_problem.clinic.in_clinic.values()
        for keys, _weekday, _session, _lit in entries
    )


def test_cp_sat_vacation_only_on_vacationable(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    for resident in instance.residents:
        for week in resident.vacation_weeks:
            assignment = next(
                item
                for item in schedule.assignments
                if item.resident_id == resident.id and week in item.weeks
            )
            assert assignment.block_duration_weeks is not None
            block = instance.rotation(assignment.rotation_id).block_config(
                resident.pgy,
                assignment.block_duration_weeks,
            )
            assert block.vacation.allowed, (resident.id, week, assignment.rotation_id)


def test_cp_sat_respects_catalog_capacity_and_rotation_grouping(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    pgy_of = {resident.id: resident.pgy for resident in instance.residents}
    by_week_rotation: dict[tuple[int, str], set[str]] = defaultdict(set)
    for assignment in schedule.assignments:
        for week in assignment.weeks:
            by_week_rotation[week, assignment.rotation_id].add(assignment.resident_id)

    for rotation in instance.rotations:
        for week in range(1, instance.calendar.weeks + 1):
            present = by_week_rotation.get((week, rotation.id), set())
            if rotation.capacity.min_concurrent is not None:
                assert len(present) >= rotation.capacity.min_concurrent, (rotation.id, week)
            if rotation.capacity.max_concurrent is not None:
                assert len(present) <= rotation.capacity.max_concurrent, (rotation.id, week)
            by_pgy: dict[int, int] = defaultdict(int)
            for resident_id in present:
                by_pgy[pgy_of[resident_id]] += 1
            for rule in rotation.pgy_rules:
                if rule.min_concurrent is not None:
                    assert by_pgy[rule.pgy] >= rule.min_concurrent, (
                        rotation.id,
                        week,
                        rule.pgy,
                    )
                if rule.max_concurrent is not None:
                    assert by_pgy[rule.pgy] <= rule.max_concurrent, (
                        rotation.id,
                        week,
                        rule.pgy,
                    )

    for resident in instance.residents:
        if resident.pgy not in {1, 2}:
            continue
        gyn = next(
            item
            for item in schedule.assignments
            if item.resident_id == resident.id and item.rotation_id == "outpatient_gyn"
        )
        labor_and_delivery = next(
            item
            for item in schedule.assignments
            if item.resident_id == resident.id and item.rotation_id == "inpatient_ld"
        )
        if resident.pgy == 1:
            assert gyn.end_week + 1 == labor_and_delivery.start_week
        else:
            assert labor_and_delivery.end_week + 1 == gyn.start_week


def test_cp_sat_unique_clinic_slots(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    unique_ids = {
        rotation.id for rotation in instance.rotations if rotation.kind is RotationKind.FMED
    }
    by_week: dict[tuple[str, int], list] = defaultdict(list)
    for assignment in schedule.assignments:
        if assignment.rotation_id not in unique_ids:
            continue
        assert assignment.kind is RotationKind.FMED
        assert assignment.clinic_slots
        key = (assignment.clinic_slots[0].weekday, assignment.clinic_slots[0].session)
        for week in assignment.weeks:
            by_week[assignment.rotation_id, week].append(key)
    for (rotation_id, week), slots in by_week.items():
        assert len(slots) == len(set(slots)), (rotation_id, week)


def _fmed_clinic_concurrency_status(
    *,
    maximum: int | None,
    maximum_by_pgy: dict[int, int],
    pgys: list[int],
    selected_slot_indexes: list[int],
):
    from ortools.sat.python import cp_model

    raw = unconfigured_sample_instance().model_dump(mode="json")
    fmed = next(rotation for rotation in raw["rotations"] if rotation["id"] == "fmed")
    fmed["clinic"]["max_concurrent"] = maximum
    fmed["clinic"]["max_concurrent_by_pgy"] = maximum_by_pgy
    instance = SchedulerInput.model_validate(raw)
    model = cp_model.CpModel()
    occurrences = []
    starts = {}
    placements = {}
    by_resident = defaultdict(list)
    residents = {}
    used_resident_ids: set[str] = set()

    for index, pgy in enumerate(pgys):
        resident = next(
            resident
            for resident in instance.residents
            if resident.pgy == pgy and resident.id not in used_resident_ids
        )
        used_resident_ids.add(resident.id)
        occurrence = Occurrence(
            key=f"fmed-concurrency-{index}",
            resident_id=resident.id,
            pgy=pgy,
            rotation_id="fmed",
            duration_weeks=1,
            group_id=f"fmed-concurrency-{index}",
        )
        present = model.NewBoolVar(f"present-{index}")
        model.Add(present == 1)
        occurrences.append(occurrence)
        starts[occurrence.key] = (1,)
        placements[occurrence.key, 1] = present
        by_resident[resident.id].append(occurrence)
        residents[resident.id] = resident

    context = PlanningContext(
        model=model,
        instance=instance,
        options=instance.solver,
        residents=residents,
        rotations=instance.rotations_by_id,
        occurrences=occurrences,
        weeks=(1,),
        starts=starts,
        placements=placements,
        by_resident=dict(by_resident),
        by_rotation={"fmed": occurrences},
    )
    decisions = fmed_kind.unique_clinic(context)
    for occurrence, selected_index in zip(
        occurrences,
        selected_slot_indexes,
        strict=True,
    ):
        model.Add(decisions[occurrence.key].selected[selected_index] == 1)

    return cp_model.CpSolver().Solve(model)


def test_cp_sat_fmed_clinic_concurrency_is_configurable_by_total_and_pgy() -> None:
    from ortools.sat.python import cp_model

    assert _fmed_clinic_concurrency_status(
        maximum=2,
        maximum_by_pgy={},
        pgys=[1, 2],
        selected_slot_indexes=[0, 0],
    ) in {cp_model.FEASIBLE, cp_model.OPTIMAL}
    assert (
        _fmed_clinic_concurrency_status(
            maximum=2,
            maximum_by_pgy={},
            pgys=[1, 1, 2],
            selected_slot_indexes=[0, 0, 0],
        )
        == cp_model.INFEASIBLE
    )
    assert (
        _fmed_clinic_concurrency_status(
            maximum=3,
            maximum_by_pgy={1: 1},
            pgys=[1, 1, 2],
            selected_slot_indexes=[0, 0, 1],
        )
        == cp_model.INFEASIBLE
    )
    assert _fmed_clinic_concurrency_status(
        maximum=3,
        maximum_by_pgy={1: 2},
        pgys=[1, 1, 2],
        selected_slot_indexes=[0, 0, 1],
    ) in {cp_model.FEASIBLE, cp_model.OPTIMAL}


def test_cp_sat_honors_block_sequencing(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    for resident in instance.residents:
        grid = schedule.week_grid[resident.id]
        weeks_by_rotation: dict[str, list[int]] = defaultdict(list)
        for week in range(1, instance.calendar.weeks + 1):
            weeks_by_rotation[grid[str(week)]].append(week)
        for rotation in instance.rotations:
            try:
                rule = rotation.pgy_rule(resident.pgy)
            except KeyError:
                continue
            this_weeks = weeks_by_rotation.get(rotation.id, [])
            if not this_weeks:
                continue
            if rule.earliest_start_week:
                assert min(this_weeks) >= rule.earliest_start_week, resident.id
            for pred in rule.prerequisite_rotation_ids:
                pred_weeks = weeks_by_rotation.get(pred, [])
                assert pred_weeks, (resident.id, rotation.id, pred)
                runs = _contiguous_runs(pred_weeks)
                assert any(max(run) < min(this_weeks) for run in runs), (
                    resident.id,
                    pred,
                    rotation.id,
                )


def test_cp_sat_consecutive_rotation_caps(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    for resident in instance.residents:
        grid = schedule.week_grid[resident.id]
        by_rotation: dict[str, list[int]] = defaultdict(list)
        for week in range(1, instance.calendar.weeks + 1):
            by_rotation[grid[str(week)]].append(week)
        for rotation in instance.rotations:
            if rotation.max_consecutive_weeks is None:
                continue
            weeks = by_rotation.get(rotation.id, [])
            if not weeks:
                continue
            longest = max(len(run) for run in _contiguous_runs(weeks))
            assert longest <= rotation.max_consecutive_weeks, (resident.id, rotation.id, longest)


def test_cp_sat_enforces_rotation_and_pgy_maximum_total_weeks() -> None:
    from ortools.sat.python import cp_model

    def solve_with_seven_limited_blocks(
        maximum: int | None,
        *,
        pgy_maximum: int | None = None,
    ) -> int:
        instance = _total_week_limit_instance(
            maximum,
            pgy_max_total_weeks=pgy_maximum,
        )
        problem = compile_problem(instance, instance.solver, cp_model)
        limited = [
            occurrence
            for occurrence in problem.context.occurrences
            if occurrence.rotation_id == "limited_elective"
        ]
        for occurrence in limited[:7]:
            problem.context.model.Add(
                sum(
                    problem.context.placements[occurrence.key, start]
                    for start in problem.context.starts[occurrence.key]
                )
                == 1
            )
        solver = cp_model.CpSolver()
        return solver.Solve(problem.context.model)

    assert solve_with_seven_limited_blocks(None) == cp_model.OPTIMAL
    assert solve_with_seven_limited_blocks(12) == cp_model.INFEASIBLE
    assert (
        solve_with_seven_limited_blocks(None, pgy_maximum=12)
        == cp_model.INFEASIBLE
    )


def _contiguous_runs(weeks: list[int]) -> list[list[int]]:
    ordered = sorted(weeks)
    runs: list[list[int]] = []
    run: list[int] = []
    for week in ordered:
        if run and week != run[-1] + 1:
            runs.append(run)
            run = []
        run.append(week)
    if run:
        runs.append(run)
    return runs


def test_clinic_kind_has_one_admin_half_day(draft) -> None:
    instance, schedule = draft
    policy = instance.clinic_policy
    clinic_ids = {
        rotation.id for rotation in instance.rotations if rotation.kind is RotationKind.CLINIC
    }
    clinic_rows = [row for row in schedule.assignments if row.rotation_id in clinic_ids]
    assert clinic_rows
    for row in clinic_rows:
        assert row.kind is RotationKind.CLINIC
        admins = [slot for slot in row.clinic_slots if slot.admin]
        assert admins, row.resident_id
        admin_days = {(slot.weekday, slot.session) for slot in admins}
        assert len(admin_days) == 1, row.resident_id
        admin = admins[0]
        assert not policy.is_academic(ClinicSlot(weekday=admin.weekday, session=admin.session))


def test_overlay_clinic_is_not_academic(draft) -> None:
    instance, schedule = draft
    policy = instance.clinic_policy
    seen = 0
    for row in schedule.assignments:
        if row.kind is RotationKind.CLINIC:
            continue
        for slot in row.clinic_slots:
            seen += 1
            assert not slot.admin
            assert not policy.is_academic(ClinicSlot(weekday=slot.weekday, session=slot.session))
            assert (slot.weekday, slot.session) != (Weekday.WEDNESDAY, Session.AFTERNOON)
    assert seen > 0


def test_solver_objective_and_final_metrics_are_separate(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    assert schedule.meta.solver_objective is not None
    peak, total = attending_load(
        instance,
        schedule,
        site=instance.clinic_policy.primary_site_id,
    )
    assert schedule.meta.metrics.primary_site_attending_sessions == total
    assert peak >= 1
    assert total >= peak
    assert any("attending-sessions" in note for note in schedule.meta.notes)
    if schedule.meta.solver_status is SolverStatus.OPTIMAL and schedule.meta.postprocessed:
        assert schedule.meta.status is SolverStatus.FEASIBLE


def test_site_capacity_and_windows(draft) -> None:
    instance, schedule = draft
    policy = instance.clinic_policy
    board = occupancy(instance, schedule)
    secondary_id = _secondary_site_id(instance)
    for week in range(1, instance.calendar.weeks + 1):
        for weekday in Weekday:
            for session in Session:
                people = board.get((week, weekday, session), [])
                assigned = [
                    person
                    for person in people
                    if occupant_site(person) == secondary_id
                ]
                assert len(assigned) <= policy.max_capacity(
                    secondary_id,
                    weekday,
                    session,
                ), (week, weekday, session)


def test_residents_follow_configured_secondary_clinic_target(draft) -> None:
    instance, schedule = draft
    policy = instance.clinic_policy
    secondary_id = _secondary_site_id(instance)
    board = occupancy(instance, schedule)

    def flex(person, week: int) -> bool:
        rotation_id = schedule.week_grid[person.resident_id][str(week)]
        rule = instance.rotation(rotation_id).clinic
        if rule is None or not rule.slots:
            return True
        return any(
            secondary_id in policy.resolve_site_ids(slot.sites)
            and policy.primary_site_id in policy.resolve_site_ids(slot.sites)
            for slot in rule.slots
        )

    by_resident: dict[str, list] = defaultdict(list)
    for (week, _weekday, _session), people in board.items():
        for person in people:
            if person.admin or not flex(person, week):
                continue
            by_resident[person.resident_id].append(occupant_site(person))
    all_sites = [site for sites in by_resident.values() for site in sites]
    assert all_sites
    target = policy.allocation(secondary_id).target_fraction
    overall = sum(1 for site in all_sites if site == secondary_id) / len(all_sites)
    assert target - 0.10 <= overall <= target + 0.10, overall
    for resident in instance.residents:
        sites = by_resident.get(resident.id, [])
        if len(sites) < 16:
            continue
        share = sum(1 for site in sites if site == secondary_id) / len(sites)
        assert target - 0.13 <= share <= target + 0.13, (
            resident.id,
            share,
            len(sites),
        )


def test_secondary_clinic_is_spread_across_weeks(draft) -> None:
    instance, schedule = draft
    secondary_id = _secondary_site_id(instance)
    by_week: dict[int, int] = defaultdict(int)
    for row in schedule.assignments:
        for slot in row.clinic_slots:
            if slot.admin or slot.site != secondary_id:
                continue
            if slot.week is None:
                continue
            by_week[slot.week] += 1
    first = sum(by_week[week] for week in range(1, 27))
    second = sum(by_week[week] for week in range(27, 53))
    total = first + second
    assert total > 0
    if total >= 40:
        assert first <= total * 0.65, (first, second, total)
        assert second <= total * 0.65, (first, second, total)


def test_primary_weekly_attending_metrics_match_final_schedule(draft) -> None:
    instance, schedule = draft
    weekly = weekly_attending_sessions(
        instance,
        schedule,
        site=instance.clinic_policy.primary_site_id,
    )
    loaded = [weekly[week] for week in range(1, instance.calendar.weeks + 1) if weekly[week] > 0]
    assert loaded
    metrics = schedule.meta.metrics
    assert metrics.primary_site_weekly_min == min(loaded)
    assert metrics.primary_site_weekly_max == max(loaded)
    assert metrics.primary_site_weekly_spread == max(loaded) - min(loaded)
    assert any("attending-sessions per week" in note for note in schedule.meta.notes)


def test_secondary_clinic_windows_are_evenly_loaded(draft) -> None:
    instance, schedule = draft
    board = occupancy(instance, schedule)
    secondary_id = _secondary_site_id(instance)
    totals = []
    for slot in instance.clinic_policy.site(secondary_id).half_days:
        count = 0
        for week in range(1, instance.calendar.weeks + 1):
            people = board[(week, slot.weekday, slot.session)]
            count += sum(
                1
                for person in people
                if occupant_site(person) == secondary_id
            )
        totals.append(count)
    total = sum(totals)
    assert total > 0
    if total >= 40:
        for count in totals:
            assert count <= total * 0.55, totals


def test_dedicated_clinic_blocks_are_spread_by_pgy(draft) -> None:
    instance, schedule = draft
    assert schedule.meta.status in {SolverStatus.OPTIMAL, SolverStatus.FEASIBLE}
    pgy_of = {resident.id: resident.pgy for resident in instance.residents}
    vacation = {resident.id: set(resident.vacation_weeks) for resident in instance.residents}
    by_week: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for row in schedule.assignments:
        if row.kind is not RotationKind.CLINIC:
            continue
        for week in row.weeks:
            if week in vacation[row.resident_id]:
                continue
            by_week[pgy_of[row.resident_id]][week] += 1
    for pgy, cohort in instance.cohort_counts().items():
        if cohort == 0:
            continue
        peak = max(by_week[pgy].values()) if by_week[pgy] else 0
        assert peak < cohort, (pgy, peak, cohort)


def test_weekly_clinic_headcount_is_even_across_the_year(draft) -> None:
    instance, schedule = draft
    board = occupancy(instance, schedule)
    by_week: dict[int, set[str]] = defaultdict(set)
    for (week, _weekday, _session), people in board.items():
        for person in people:
            if person.admin:
                continue
            by_week[week].add(person.resident_id)
    counts = [len(by_week[week]) for week in range(1, instance.calendar.weeks + 1)]
    assert max(counts) >= 1
    assert max(counts) - min(counts) <= 8, (min(counts), max(counts), counts[:12])


def test_clinic_load_spreads_across_weekdays(draft) -> None:
    instance, schedule = draft
    board = occupancy(instance, schedule)
    policy = instance.clinic_policy
    by_day = {day: 0 for day in WEEKDAYS_MF}
    for week in range(1, instance.calendar.weeks + 1):
        for weekday in WEEKDAYS_MF:
            for session in Session:
                if is_academic(policy, weekday, session):
                    continue
                by_day[weekday] += clinic_headcount(board[(week, weekday, session)])
    monday = by_day[Weekday.MONDAY]
    later = by_day[Weekday.THURSDAY] + by_day[Weekday.FRIDAY]
    assert later >= monday * 0.4, by_day


def test_busy_clinic_sessions_mix_pgy_years(draft) -> None:
    instance, schedule = draft
    board = occupancy(instance, schedule)
    mixed = 0
    single = 0
    for people in board.values():
        clinic = [person for person in people if not person.admin]
        if len(clinic) < 4:
            continue
        years = {person.pgy for person in clinic}
        if len(years) >= 2:
            mixed += 1
        else:
            single += 1
    assert mixed + single > 0
    assert mixed >= single


def test_clinic_block_bounds_hold_every_week() -> None:
    from collections import Counter

    instance = sample_instance()
    solver = instance.solver.model_copy(
        update={
            "time_limit_seconds": 30,
            "random_seed": 1,
            "min_clinic_blocks_per_week": 6,
            "max_clinic_blocks_per_week": 8,
        }
    )
    schedule = get_engine("cp_sat").solve(
        instance.revised(solver=solver),
        options=solver,
    )

    assert not schedule.is_empty()
    on_clinic: Counter = Counter()
    for assignment in schedule.assignments:
        if assignment.kind is RotationKind.CLINIC:
            for week in assignment.weeks:
                on_clinic[week] += 1
    per_week = [on_clinic.get(week, 0) for week in range(1, instance.calendar.weeks + 1)]

    assert min(per_week) >= 6
    assert max(per_week) <= 8
    assert schedule.meta.metrics.clinic_block_weekly_spread <= 2
    assert schedule.meta.metrics.elective_fallback_blocks > 0


def test_clinic_balance_is_automatic_by_default() -> None:
    from rbs.solver.planning import clinic_block_week_band, resolve_clinic_block_band

    instance = sample_instance()

    assert instance.solver.auto_balance_clinic_blocks is True
    assert instance.solver.min_clinic_blocks_per_week is None
    assert instance.solver.max_clinic_blocks_per_week is None

    low, high, automatic = resolve_clinic_block_band(instance, instance.solver)
    assert (low, high) == clinic_block_week_band(instance)
    assert automatic is True


def test_explicit_clinic_bounds_override_the_automatic_band() -> None:
    from rbs.solver.planning import resolve_clinic_block_band

    instance = sample_instance()
    tuned = instance.revised(
        solver=instance.solver.model_copy(update={"max_clinic_blocks_per_week": 9})
    )

    assert resolve_clinic_block_band(tuned, tuned.solver) == (None, 9, False)


def test_disabling_automatic_balance_removes_the_band() -> None:
    from rbs.solver.planning import resolve_clinic_block_band

    instance = sample_instance()
    tuned = instance.revised(
        solver=instance.solver.model_copy(update={"auto_balance_clinic_blocks": False})
    )

    assert resolve_clinic_block_band(tuned, tuned.solver) == (None, None, False)


def test_unsatisfiable_automatic_band_is_dropped_rather_than_failing(monkeypatch) -> None:
    import rbs.solver.planning as planning

    instance = sample_instance()
    solver = instance.solver.model_copy(
        update={"time_limit_seconds": 30, "random_seed": 1}
    )
    instance = instance.revised(solver=solver)

    # a floor no curriculum could meet: every resident on Clinic every week
    monkeypatch.setattr(
        planning,
        "clinic_block_week_band",
        lambda _instance: (len(instance.residents), len(instance.residents)),
    )

    schedule = get_engine("cp_sat").solve(instance, options=instance.solver)

    assert not schedule.is_empty()
    assert any("automatic clinic balance" in note for note in schedule.meta.notes)


def test_objective_weights_survive_a_case_round_trip() -> None:
    instance = sample_instance()
    tuned = instance.revised(
        solver=instance.solver.model_copy(
            update={
                "weights": instance.solver.weights.model_copy(
                    update={"attending_sessions": 17}
                )
            }
        )
    )

    restored = SchedulerInput.model_validate(tuned.model_dump(mode="json"))

    assert restored.solver.weights.attending_sessions == 17
    assert restored.solver.weights.preferred_clinic_slots == (
        instance.solver.weights.preferred_clinic_slots
    )
    assert restored.solver.weights.clinic_block_week_evenness == (
        instance.solver.weights.clinic_block_week_evenness
    )


def test_clinic_block_week_band_matches_the_curriculum() -> None:
    from rbs.solver.planning import clinic_block_week_band

    instance = sample_instance()
    total = sum(
        occurrence.duration_weeks
        for occurrence in expand_occurrences(instance)
        if instance.rotation(occurrence.rotation_id).kind is RotationKind.CLINIC
        and not occurrence.elective_fallback
    )
    low, high = clinic_block_week_band(instance)
    weeks = instance.calendar.weeks

    # the band brackets the average, and the ceiling can always absorb the total
    assert low * weeks <= total <= high * weeks
    assert high - low <= 1


def test_suggested_band_keeps_every_week_staffed() -> None:
    from collections import Counter

    from rbs.solver.planning import clinic_block_week_band

    instance = sample_instance()
    low, high = clinic_block_week_band(instance)
    solver = instance.solver.model_copy(
        update={
            "time_limit_seconds": 30,
            "random_seed": 1,
        }
    )
    schedule = get_engine("cp_sat").solve(
        instance.revised(solver=solver),
        options=solver,
    )

    assert not schedule.is_empty()
    on_clinic: Counter = Counter()
    for assignment in schedule.assignments:
        if assignment.kind is RotationKind.CLINIC and not assignment.elective_fallback:
            for week in assignment.weeks:
                on_clinic[week] += 1

    # the final weeks are the ones an attending-minimising objective empties first
    for week in range(1, instance.calendar.weeks + 1):
        assert on_clinic.get(week, 0) >= low, f"week {week} has no Clinic block"


def test_portfolio_plan_splits_the_worker_budget() -> None:
    from rbs.models.instance import SolverConfig
    from rbs.solver.tuning import MIN_WORKERS_PER_ATTEMPT, portfolio_plan

    def plan(workers, attempts):
        return portfolio_plan(SolverConfig(num_workers=workers, solve_attempts=attempts))

    assert plan(12, 3) == (3, 4)
    assert plan(9, 3) == (3, 3)
    assert plan(24, 3) == (3, 8)
    # one attempt keeps the whole budget
    assert plan(12, 1) == (1, 12)
    # never split so thin that an attempt cannot search: 2 workers collapsed
    # 5 of 6 measured runs, so a small budget stays a single solve
    assert plan(8, 3) == (2, 4)
    assert plan(4, 3) == (1, 4)
    assert plan(2, 3) == (1, 2)
    assert plan(1, 3) == (1, 1)
    for workers in range(1, 33):
        attempts, each = plan(workers, 4)
        assert attempts == 1 or each >= MIN_WORKERS_PER_ATTEMPT


def test_attempt_rank_prefers_usable_then_lower_objective() -> None:
    from rbs.models.enums import SolverEngineName
    from rbs.solver.core.base import empty_schedule
    from rbs.solver.core.cp_sat import _attempt_rank

    instance = sample_instance()
    blank = empty_schedule(
        instance,
        engine=SolverEngineName.CP_SAT,
        status=SolverStatus.UNKNOWN,
        notes=[],
        wall_time_seconds=1.0,
    )
    options = instance.solver.model_copy(
        update={"time_limit_seconds": 20, "solve_attempts": 1, "random_seed": 1}
    )
    schedule = get_engine("cp_sat").solve(
        instance.revised(solver=options),
        options=options,
    )

    assert _attempt_rank(schedule) < _attempt_rank(blank)


def test_portfolio_records_how_the_budget_was_spent() -> None:
    instance = sample_instance()
    tuned = instance.revised(
        solver=instance.solver.model_copy(
            update={
                "time_limit_seconds": 20,
                "num_workers": 12,
                "solve_attempts": 3,
                "random_seed": 1,
            }
        )
    )

    schedule = get_engine("cp_sat").solve(tuned, options=tuned.solver)

    assert not schedule.is_empty()
    assert any("3 concurrent solves" in note for note in schedule.meta.notes)
