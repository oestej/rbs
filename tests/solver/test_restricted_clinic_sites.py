"""Restricted sessions consume seats only at sites they can actually use."""

import json
from collections import Counter

import pytest
from ortools.sat.python import cp_model

from rbs.catalog import blank_instance
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
from rbs.solver.clinic_requirements import missing_required_clinic_sessions
from rbs.solver.core.clinic_allocation import assign_clinic_sites
from rbs.solver.core.compile import compile_problem
from rbs.solver.validation import validate_schedule

MON, AM = Weekday.MONDAY, Session.MORNING


def _instance(
    domains=(('a', 'b'),), capacities=(1, 1, 20), *, points=1, managed=False, template=False,
):
    raw = blank_instance().model_dump(mode="json")
    clinic = raw["rotations"][0]
    clinic["pgy_rules"][0]["capacity_per_resident"] = points
    service = {
        **raw["rotations"][1], "id": "service", "code": "SRV", "name": "Service",
        "kind": "standard", "clinic": None,
    }
    other = {**service, "id": "other", "code": "OTH", "name": "Other service"}
    raw["rotations"] = [clinic, service, other]
    raw["requirements"][0]["blocks"] = [
        {"rotation_id": "service", "duration_weeks": 4, "count": 6},
        {"rotation_id": "other", "duration_weeks": 4, "count": 7},
    ]
    raw["residents"] = [
        {"id": f"r{index}", "name": f"Resident {index}", "pgy": 1,
         "clinic_half_days": [{"weekday": "monday", "session": "morning", "sites": sites}]}
        for index, sites in enumerate(domains)
    ]
    if template:
        for person in raw["residents"]:
            person["clinic_half_days"] = []
        for rotation in raw["rotations"][1:]:
            rotation["clinic"] = {
                "half_days_per_week": 1,
                "slots": [{"weekday": "monday", "session": "morning", "sites": domains[0]}],
            }
    raw["attendings"] = []
    raw["attending_schedules"] = []
    raw["clinic_policy"]["sites"] = [
        {"id": site, "name": site.upper(), "color": "#0058A2", "residents_per_attending": 1,
         "staffing_mode": "attending_managed" if managed and site in ('a', 'b')
         else "capacity_managed",
         "half_days": [{"weekday": "monday", "session": "morning", "attendings": capacity}]
         if capacity else [],
         "allocation_rules": [{"clinic_id": site, "target_percent": target}]}
        for site, capacity, target in zip(('a', 'b', 'c'), capacities, (100, 0, 0), strict=True)
    ]
    raw["clinic_policy"]["primary_site_id"] = "a"
    raw["clinic_policy"]["allocation_rules"] = [
        rule for site in raw["clinic_policy"]["sites"] for rule in site["allocation_rules"]
    ]
    if managed:
        raw["attendings"] = [{
            "id": "preceptor", "name": "Preceptor", "half_days_per_week": 2,
            "eligible_clinic_ids": ["a", "b"],
            "weekly_shift_targets": [{"work_type": "precepting_clinic", "mode": "flexible",
                                      "minimum_shifts_per_week": 0, "maximum_shifts_per_week": 0}],
        }]
    return SchedulerInput.model_validate(raw)


def _assignments(instance):
    return [Assignment(
        resident_id=resident.id, rotation_id="service" if index % 2 else "other",
        kind=RotationKind.STANDARD, start_week=start, end_week=start + 3,
        weeks=list(range(start, start + 4)), block_start_week=start, block_duration_weeks=4,
        clinic_slots=[AssignedClinic(week=week, weekday=MON, session=AM,
                                     allowed_sites=(resident.clinic_half_days[0].sites
                                                    if resident.clinic_half_days else ['a', 'b']))
                      for week in range(start, start + 4)],
    ) for resident in instance.residents for index, start in enumerate(range(1, 53, 4))]


def _schedule(instance, assignments):
    return Schedule(meta=ScheduleMeta(
        academic_year=instance.academic_year, engine="cp_sat", status="feasible",
    ), assignments=assignments)


def test_allocator_preserves_sessions_without_staffing_at_their_allowed_sites():
    instance = _instance(capacities=(0, 0, 20))
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    assert sum(len(assignment.clinic_slots) for assignment in assignments) == 52
    assert all(slot.site in ('a', 'b') for assignment in assignments
               for slot in assignment.clinic_slots)
    result = validate_schedule(
        SolverProblem.from_instance(instance), _schedule(instance, assignments),
    )
    assert not result.valid
    assert any("no attending coverage" in error for error in result.errors)


@pytest.mark.parametrize("template", [False, True])
def test_validation_rejects_a_required_session_deleted_after_allocation(template):
    instance = _instance(template=template)
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    assignments[-1].clinic_slots.pop()
    result = validate_schedule(
        SolverProblem.from_instance(instance), _schedule(instance, assignments),
    )
    assert not result.valid
    assert any("required clinic session missing" in error and "week 52" in error
               for error in result.errors)


def test_validation_checks_source_sites_instead_of_trusting_output_metadata():
    instance = _instance()
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    assignments[0].clinic_slots[0].site = 'c'
    assignments[0].clinic_slots[0].allowed_sites = ['c']
    result = validate_schedule(
        SolverProblem.from_instance(instance), _schedule(instance, assignments),
    )
    assert any("clinic site c is not allowed" in error for error in result.errors)


def test_an_incomplete_solve_cannot_replace_the_saved_schedule(tmp_path):
    from rbs.store import Store
    from rbs.workspaces import WorkspaceController

    instance = _instance(template=True)
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    store = Store(tmp_path / 'restricted-sites.sqlite')
    store.init()
    workspace = store.create('Restricted sites', instance, _schedule(instance, assignments))
    damaged = workspace.latest_schedule.revised(assignments=[
        assignment.revised(clinic_slots=assignment.clinic_slots[:-1])
        if index == 0 else assignment
        for index, assignment in enumerate(workspace.latest_schedule.assignments)
    ])
    with pytest.raises(ValueError, match='required clinic session missing'):
        WorkspaceController(store).save_schedule(workspace, damaged)
    assert store.get(workspace.id) == workspace


def test_an_older_file_with_a_missing_session_opens_as_out_of_date(tmp_path):
    from rbs.store import Store

    instance = _instance(template=True)
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    store = Store(tmp_path / 'legacy-sites.sqlite')
    store.init()
    workspace = store.create('Legacy result', instance, _schedule(instance, assignments))
    document = json.loads(store.export_workspace_rbsc(workspace.id))
    document['workspaces'][0]['schedule']['assignments'][0]['clinic_slots'].pop()
    reopened = store.import_workspace_rbsc(json.dumps(document))[0]
    assert reopened.solution_is_out_of_date
    assert reopened.schedule is None
    assert len(reopened.latest_schedule.assignments) == len(assignments)
    assert sum(len(item.clinic_slots) for item in reopened.latest_schedule.assignments) == 51
    # Tolerance for an older missing session never permits dangling identities.
    document['workspaces'][0]['schedule']['assignments'][0]['resident_id'] = 'missing'
    with pytest.raises(ValueError, match='unknown resident'):
        store.import_workspace_rbsc(json.dumps(document))


@pytest.mark.parametrize("deadline", [None, 0.0])
def test_allocator_repairs_a_greedy_dead_end_without_moving_a_lock(deadline):
    instance = _instance(domains=[['a', 'b', 'c'], ['a', 'b'], ['a', 'b']], capacities=(1, 1, 1))
    assignments = _assignments(instance)
    # A locked first resident at C leaves exactly A and B for restricted residents.
    assignments[0].clinic_slots[0].site = 'c'
    assignments[0].clinic_slots[0].locked = True
    result = assign_clinic_sites(instance, assignments, deadline=deadline)
    assert sum(result.assigned_by_clinic.values()) == 156
    for week in range(1, 53):
        assert Counter(slot.site for assignment in assignments for slot in assignment.clinic_slots
                       if slot.week == week) == {'a': 1, 'b': 1, 'c': 1}
    assert assignments[0].clinic_slots[0].locked
    assert validate_schedule(SolverProblem.from_instance(instance),
                             _schedule(instance, assignments)).valid


@pytest.mark.parametrize("points,capacities,count", [(1, (1, 1, 20), 3), (2, (3, 3, 20), 3)])
def test_compiler_rejects_overflow_even_with_spare_seats_at_an_unusable_site(
    points, capacities, count,
):
    instance = _instance(domains=[['a', 'b']] * count, capacities=capacities, points=points)
    compiled = compile_problem(SolverProblem.from_instance(instance), instance.solver, cp_model)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.Solve(compiled.context.model) == cp_model.INFEASIBLE


def test_closure_cancels_only_sessions_with_no_open_allowed_site():
    instance = _instance()
    day = instance.calendar.first_week_start
    raw = instance.model_dump(mode='json')
    raw['clinic_policy']['closure_days'] = [
        {'date': day.isoformat(), 'sites': ['a', 'b'], 'name': 'Closure'},
    ]
    instance = SchedulerInput.model_validate(raw)
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    assert sum(len(assignment.clinic_slots) for assignment in assignments) == 51
    assert missing_required_clinic_sessions(instance, _schedule(instance, assignments)) == []


def test_a_directory_wide_closed_half_day_cancels_sessions_without_creating_a_shortfall():
    instance = _instance(template=True)
    raw = instance.model_dump(mode='json')
    for rotation in raw['rotations'][1:]:
        rotation['clinic']['slots'].append(
            {'weekday': 'tuesday', 'session': 'morning', 'sites': ['a', 'b']},
        )
        rotation['clinic']['half_days_per_week'] = 2
    instance = SchedulerInput.model_validate(raw)
    assignments = _assignments(instance)
    for assignment in assignments:
        assignment.clinic_slots.extend(AssignedClinic(
            week=week, weekday=Weekday.TUESDAY, session=AM, allowed_sites=['a', 'b'],
        ) for week in assignment.weeks)
    assign_clinic_sites(instance, assignments)
    assert sum(len(assignment.clinic_slots) for assignment in assignments) == 52
    assert validate_schedule(SolverProblem.from_instance(instance),
                             _schedule(instance, assignments)).valid


def test_zero_rotation_target_does_not_require_its_retained_slot_choices():
    instance = _instance(template=True)
    raw = instance.model_dump(mode='json')
    for rotation in raw['rotations'][1:]:
        rotation['clinic']['half_days_per_week'] = 0
    instance = SchedulerInput.model_validate(raw)
    assignments = [assignment.revised(clinic_slots=[]) for assignment in _assignments(instance)]
    assert validate_schedule(SolverProblem.from_instance(instance),
                             _schedule(instance, assignments)).valid


def test_hand_entered_precepting_can_open_a_managed_clinic_without_recurring_coverage():
    instance = _instance(managed=True, capacities=(0, 0, 0))
    raw = instance.model_dump(mode='json')
    raw['attending_schedules'] = [{
        'attending_id': 'preceptor', 'weeks': [{
            'week': 1, 'half_days': [{
                'weekday': 'monday', 'session': 'morning',
                'work_type': 'precepting_clinic', 'clinic_id': 'a',
            }],
        }],
    }]
    instance = SchedulerInput.model_validate(raw)
    assignments = _assignments(instance)
    assign_clinic_sites(instance, assignments)
    slots = [slot for assignment in assignments for slot in assignment.clinic_slots]
    assert [(slot.week, slot.site) for slot in slots] == [(1, 'a')]
    assert validate_schedule(SolverProblem.from_instance(instance),
                             _schedule(instance, assignments)).valid


@pytest.mark.solve
@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("template", [False, True])
def test_public_solve_keeps_restricted_sessions_and_staffs_only_usable_sites(managed, template):
    from rbs.solver import solve_problem

    instance = _instance(managed=managed, template=template)
    schedule = solve_problem(
        SolverProblem.from_instance(instance),
        options=instance.solver.revised(time_limit_seconds=10, num_workers=1),
    )
    assert schedule.meta.status.value in ('optimal', 'feasible')
    slots = [slot for assignment in schedule.assignments for slot in assignment.clinic_slots]
    assert len(slots) == 52
    assert all(slot.site in ('a', 'b') and slot.allowed_sites == ['a', 'b'] for slot in slots)
    assert validate_schedule(SolverProblem.from_instance(instance), schedule).valid
    if managed:
        assert len(schedule.attending_work) == 52
        assert all(item.clinic_id in ('a', 'b') for item in schedule.attending_work)


@pytest.mark.solve
@pytest.mark.parametrize('managed', [False, True])
def test_search_seating_repairs_mixed_weights_even_after_the_deadline(monkeypatch, managed):
    from rbs.solver.core import clinic_allocation
    from rbs.solver.core.clinic_seating import solved_seating
    from rbs.solver.core.decode import solved_capacity_view

    instance = _instance(
        domains=[['a', 'b', 'c'], ['a', 'b'], ['b', 'a']],
        capacities=(2, 2, 1), points=2, managed=managed,
    )
    raw = instance.model_dump(mode='json')
    raw['residents'][0]['pgy'] = 2
    raw['requirements'].append({**raw['requirements'][0], 'pgy': 2,
                                'code': 'LVL2', 'label': 'Second level'})
    for rotation in raw['rotations']:
        rotation['pgy_rules'].append({**rotation['pgy_rules'][0], 'pgy': 2,
                                     'capacity_per_resident': 1})
    if managed:
        person = raw['attendings'][0]
        raw['attendings'] = [{**person, 'id': f'preceptor-{index}'} for index in range(4)]
    instance = SchedulerInput.model_validate(raw)
    compiled = compile_problem(SolverProblem.from_instance(instance), instance.solver, cp_model)
    compiled = compiled.clone_for_search()
    compiled.context.model.ClearObjective()
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.Solve(compiled.context.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    seating = {key: solved_seating(plans, solver)
               for key, plans in compiled.clinic.site_seating.items()}
    polished = {}
    capacity = solved_capacity_view(compiled, solver, seating=polished)
    seating.update(polished)
    assignments = _assignments(instance)
    # Make the first, light resident take A during the target pass. The two
    # heavier A/B residents then need a rearrangement that puts the first at C.
    monkeypatch.setattr(clinic_allocation, '_target_assignment_key',
                        lambda candidate, *_args: (candidate.resident_id, candidate.slot.week))
    assign_clinic_sites(capacity, assignments, deadline=0.0, seating=seating)
    assert all(slot.site == 'c' for assignment in assignments if assignment.resident_id == 'r0'
               for slot in assignment.clinic_slots)
    assert validate_schedule(capacity, _schedule(instance, assignments)).valid
