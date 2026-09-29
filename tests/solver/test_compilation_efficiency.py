"""Compilation caches and optional quality terms preserve scheduling rules."""

from collections import Counter
from datetime import timedelta

import pytest
from ortools.sat.python import cp_model

from rbs.models.case_blocks import AcademicHalfDayOverride
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.solver_options import ObjectiveWeights
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.solver.core import get_engine
from rbs.solver.core.compile import compile_problem
from rbs.solver.core.context import PlanningContext
from rbs.solver.core.kinds import clinic


def _context(instance):
    return PlanningContext.compile(
        SolverProblem.from_instance(instance), instance.solver, cp_model
    )


def _zero_weights():
    return ObjectiveWeights(**dict.fromkeys(ObjectiveWeights.model_fields, 0))


def test_clinic_domains_are_reused_only_within_the_same_compilation(instance, monkeypatch):
    instance = instance.revised(academic_half_day_overrides=[])
    context = _context(instance)
    rotation = next(r for r in context.rotations.values() if r.kind is RotationKind.CLINIC)
    calls = Counter()
    original = clinic.week_domain

    def counted(problem, week, rotation=None):
        calls[week, rotation.id if rotation else None] += 1
        return original(problem, week, rotation)

    monkeypatch.setattr(clinic, "week_domain", counted)
    before = clinic.cached_week_domain(context, 11, rotation)
    assert clinic.cached_week_domain(context, 11, rotation) is before
    clinic.cached_week_domain(context, 12, rotation)
    clinic.cached_week_domain(context, 11)
    assert calls == {(11, rotation.id): 1, (12, rotation.id): 1, (11, None): 1}

    edited = instance.revised(
        academic_half_day_overrides=[
            AcademicHalfDayOverride(week=11, weekday=Weekday.TUESDAY, session=Session.MORNING)
        ]
    )
    after_context = _context(edited)
    after = clinic.cached_week_domain(after_context, 11, after_context.rotations[rotation.id])
    tuesday = Weekday.TUESDAY, Session.MORNING
    assert tuesday in {(slot.weekday, slot.session) for slot in before}
    assert tuesday not in {(slot.weekday, slot.session) for slot in after}
    assert calls[11, rotation.id] == 2
    assert clinic.cached_week_domain(context, 11, rotation) is before


def test_availability_cache_keeps_residents_sessions_and_edits_separate(instance, monkeypatch):
    instance = instance.revised(
        residents=[resident.revised(days_off=[]) for resident in instance.residents],
        special_rotations=[],
    )
    context = _context(instance)
    first, second = instance.residents[:2]
    calls = Counter()
    original = SolverProblem.resident_clinic_is_blocked

    def counted(problem, resident_id, week, weekday, session=None):
        calls[resident_id, week, weekday, session] += 1
        return original(problem, resident_id, week, weekday, session)

    monkeypatch.setattr(SolverProblem, "resident_clinic_is_blocked", counted)
    key = first.id, 11, Weekday.FRIDAY, Session.MORNING
    assert context.resident_clinic_is_blocked(*key) is False
    assert context.resident_clinic_is_blocked(*key) is False
    assert calls[key] == 1

    friday = instance.calendar.first_week_start + timedelta(weeks=10, days=4)
    edited = instance.revised(
        residents=[first.revised(days_off=[friday]), *instance.residents[1:]],
        special_rotations=[
            SpecialRotation(
                id="exam",
                name="Exam",
                kind=SpecialRotationKind.EVENT,
                start_date=friday,
                end_date=friday,
                session=Session.MORNING,
                resident_ids=[second.id],
            )
        ],
    )
    after = _context(edited)
    assert after.resident_clinic_is_blocked(*key) is True
    assert calls[key] == 2
    assert after.resident_clinic_is_blocked(second.id, 11, Weekday.FRIDAY, Session.MORNING)
    assert not after.resident_clinic_is_blocked(
        second.id, 11, Weekday.FRIDAY, Session.AFTERNOON
    )
    assert not after.resident_clinic_is_blocked(second.id, 12, Weekday.FRIDAY, Session.MORNING)
    assert context.resident_clinic_is_blocked(*key) is False


GOAL_PREFIXES = {
    "attending_sessions": {"primary_n", "att"},
    "preferred_clinic_slots": set(),  # Uses the existing clinic choice literals.
    "clinic_block_week_evenness": {"ckweek", "ckweek_year"},
    "clinic_kind_pgy_spread": {"ck", "ckspread"},
    "within_week_evenness": {"slot_n", "weekslots", "day_n", "weekdays"},
    "primary_site_week_evenness": {"primary_n", "att", "primary_att_week", "primary_att_weeks"},
    "session_pgy_mix": {"pgy1", "pgy2", "pgy3", "pgymix"},
}
QUALITY_PREFIXES = set().union(*GOAL_PREFIXES.values())


@pytest.mark.parametrize("goal", [None, *GOAL_PREFIXES])
def test_disabled_goals_are_pruned_and_enabled_goals_keep_their_dependencies(instance, goal):
    # Give the preferred-half-day goal a nonconstant cost, too.
    rotation = instance.rotation("fmed")
    rule = rotation.clinic.revised(
        slots=[
            slot.revised(preferred=index == 0)
            for index, slot in enumerate(rotation.clinic.slots)
        ]
    )
    instance = instance.revised(
        rotations=[r.revised(clinic=rule) if r.id == rotation.id else r for r in instance.rotations]
    )
    weights = _zero_weights()
    if goal is not None:
        weights = weights.revised(**{goal: 1})
    options = instance.solver.revised(weights=weights)
    compiled = compile_problem(SolverProblem.from_instance(instance), options, cp_model)
    prefixes = {
        variable.name.split(":")[0] for variable in compiled.context.model.Proto().variables
    }

    assert prefixes & QUALITY_PREFIXES == (GOAL_PREFIXES[goal] if goal else set())
    assert compiled.clinic.has_objective is (goal is not None)
    assert (compiled.clinic.quality_bound > 0) is (goal is not None)
    # Clinic choices and occupancy are still hard inputs when quality is off.
    assert compiled.clinic.decisions
    assert compiled.clinic.in_clinic
    assert not compiled.context.model.Validate()


def _clinic_only_instance(instance):
    raw = instance.model_dump(mode="json")
    resident = raw["residents"][0]
    resident.update(vacation_weeks=[], days_off=[], clinic_half_days=[], elective_preferences=[])
    rotation = next(r for r in raw["rotations"] if r["kind"] == "clinic")
    alternate = {**rotation, "id": "alternate-clinic", "code": "ALT", "name": "Alternate clinic"}
    raw.update(
        residents=[resident],
        rotations=[rotation, alternate],
        requirements=[
            {
                **curriculum,
                "blocks": [
                    dict(rotation_id=r["id"], duration_weeks=2, count=13)
                    for r in (rotation, alternate)
                ],
            }
            for curriculum in raw["requirements"]
        ],
        electives={**raw["electives"], "rotation_options": []},
        rotation_groups=[],
        special_rotations=[],
        academic_half_day_overrides=[],
        locks=[],
    )
    result = SchedulerInput.model_validate(raw)
    return result.revised(
        solver=result.solver.revised(
            weights=_zero_weights(),
            time_limit_seconds=5,
            solve_attempts=1,
            num_workers=1,
            auto_balance_clinic_blocks=False,
        )
    )


@pytest.mark.solve
def test_disabling_quality_preserves_clinic_locks_stability_and_metrics_after_an_edit(instance):
    instance = _clinic_only_instance(instance)
    engine = get_engine("cp_sat")
    reference = engine.solve(SolverProblem.from_instance(instance), options=instance.solver)
    assert not reference.is_empty()
    assert not reference.meta.validation_errors
    assert reference.meta.solver_objective is None
    assert reference.meta.metrics.primary_site_attending_sessions > 0

    locked = next(slot for slot in reference.assignments[0].clinic_slots if not slot.admin)
    locked.locked = True
    resident = instance.residents[0]
    # The next week's day off must be respected without dropping the existing lock.
    day_off = instance.calendar.first_week_start + timedelta(weeks=1, days=0)
    edited = instance.revised(residents=[resident.revised(days_off=[day_off])])
    result = engine.solve(
        SolverProblem.from_instance(edited), options=edited.solver, reference_schedule=reference
    )

    assert not result.is_empty()
    assert not result.meta.validation_errors
    assert result.meta.solver_objective is not None  # Stability remains an objective.
    assert result.week_grid == reference.week_grid
    slots = [slot for assignment in result.assignments for slot in assignment.clinic_slots]
    assert any(
        (slot.week, slot.weekday, slot.session) == (locked.week, locked.weekday, locked.session)
        and slot.locked
        for slot in slots
    )
    assert not any(slot.week == 2 and slot.weekday is Weekday.MONDAY for slot in slots)
    assert result.meta.metrics.primary_site_attending_sessions > 0
