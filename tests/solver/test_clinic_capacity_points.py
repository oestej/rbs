"""Capacity weights agree at persistence, compilation, allocation and display boundaries."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rbs.catalog import sample_instance
from rbs.models.catalog import ConstraintCatalog
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import PGYRotationRule
from rbs.models.schedule import AssignedClinic, Assignment, Schedule
from rbs.solver.core.clinic_allocation import assign_clinic_sites, clinic_weekly_attendings
from rbs.solver.validation_placement import clinic_capacity_violations
from rbs.ui.clinic.projection import attending_load


def weighted_instance():
    raw = sample_instance().model_dump(mode="json")
    for rotation in raw["rotations"]:
        if rotation["kind"] == "clinic":
            for rule in rotation["pgy_rules"]:
                rule["capacity_per_resident"] = 3
    return SchedulerInput.model_validate(raw)


def test_capacity_defaults_and_validated_roundtrip():
    instance = weighted_instance()
    catalog = ConstraintCatalog.from_instance(instance)
    assert ConstraintCatalog.model_validate_json(catalog.model_dump_json()) == catalog
    raw = catalog.model_dump(mode="json")
    raw["schema_version"] = 8
    for rotation in raw["rotations"]:
        for rule in rotation["pgy_rules"]:
            rule.pop("capacity_per_resident")
    migrated = ConstraintCatalog.model_validate(raw)
    assert migrated.schema_version == 9
    assert all(r.capacity_per_resident == 1 for rot in migrated.rotations for r in rot.pgy_rules)
    for version in (7, 10):
        with pytest.raises(ValidationError):
            ConstraintCatalog.model_validate({**raw, "schema_version": version})
    rule = instance.rotations[0].pgy_rules[0].model_dump()
    with pytest.raises(ValidationError):
        PGYRotationRule.model_validate({**rule, "capacity_per_resident": 0})


def test_allocation_and_attending_totals_use_points():
    instance = weighted_instance()
    rotation = next(r for r in instance.rotations if r.kind == RotationKind.CLINIC)
    assignments = [
        Assignment(
            resident_id=resident.id,
            rotation_id=rotation.id,
            kind=rotation.kind,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    week=1,
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                )
            ],
        )
        for resident in instance.residents[:4]
    ]
    result = assign_clinic_sites(instance, assignments)
    schedule = Schedule(
        meta=dict(academic_year=instance.academic_year, engine="stub", status="feasible"),
        assignments=assignments,
    )
    assert sum(result.assigned_by_clinic.values()) == 4  # Allocation still counts sessions.
    assert not clinic_capacity_violations(instance, schedule)
    for site in instance.clinic_policy.sites:
        people = sum(a.clinic_slots[0].site == site.id for a in assignments)
        expected = instance.clinic_policy.attendings_needed(people * 3, site.id)
        assert clinic_weekly_attendings(instance, assignments, site.id).get(1, 0) == expected
        assert attending_load(instance, schedule, site=site.id) == (expected, expected)
    # Four residents fit a four-resident limit, but their twelve points do not.
    for assignment in assignments:
        assignment.clinic_slots[0].site = "maple"
    violations = clinic_capacity_violations(instance, schedule)
    assert len(violations) == 1
    assert "12 capacity points" in violations[0].message


def test_compilation_weights_clinic_attendance_on_every_rotation():
    from collections import defaultdict

    from ortools.sat.python import cp_model

    from rbs.solver.core.objective_entries import _materialize_week_entries

    instance = weighted_instance()
    resident = instance.residents[0]
    clinic = next(r for r in instance.rotations if r.kind == RotationKind.CLINIC)
    standard = next(r for r in instance.rotations if r.kind == RotationKind.STANDARD)
    model = cp_model.CpModel()
    context = SimpleNamespace(
        model=model,
        instance=instance,
        residents=instance.residents_by_id,
        rotations={r.id: r for r in instance.rotations},
    )
    literals = [model.NewBoolVar("clinic"), model.NewBoolVar("standard")]
    members = [
        (
            SimpleNamespace(pgy=resident.pgy, key=r.id, rotation_id=r.id),
            Weekday.TUESDAY,
            Session.MORNING,
            literal,
            None,
        )
        for r, literal in zip([clinic, standard], literals, strict=True)
    ]
    state = SimpleNamespace(in_clinic=defaultdict(list))
    groups = _materialize_week_entries(
        context,
        1,
        {(resident.id, Weekday.TUESDAY, Session.MORNING, None): members},
        state,
    )
    points = groups[4][Weekday.TUESDAY, Session.MORNING]
    assert [p.index for p in points].count(literals[0].index) == 3
    assert [p.index for p in points].count(literals[1].index) == 3
    assert len(groups[1][Weekday.TUESDAY, Session.MORNING]) == 2


@pytest.mark.solve
def test_weighted_clinic_capacity_rejects_overflow_on_every_rotation():
    """Changing rotation must not bypass the resident's clinic capacity requirement."""
    from collections import defaultdict

    from ortools.sat.python import cp_model

    from rbs.solver.core.objective_entries import (
        _add_half_day_capacity,
        _materialize_week_entries,
    )
    from rbs.solver.core.objective_slots import _Conditional

    raw = weighted_instance().model_dump(mode="json")
    for rotation in raw["rotations"]:
        if rotation["kind"] == "clinic":
            for rule in rotation["pgy_rules"]:
                rule["capacity_per_resident"] = 17
    instance = SchedulerInput.model_validate(raw)
    resident = instance.residents[0]
    clinic = next(r for r in instance.rotations if r.kind == RotationKind.CLINIC)
    standard = next(r for r in instance.rotations if r.kind == RotationKind.STANDARD)
    model = cp_model.CpModel()
    clinic_present = model.NewBoolVar("clinic")
    standard_present = model.NewBoolVar("standard")
    session_selected = model.NewBoolVar("session")
    model.Add(clinic_present + standard_present == 1)
    model.Add(session_selected == 1)
    context = SimpleNamespace(
        model=model,
        instance=instance,
        residents=instance.residents_by_id,
        rotations={r.id: r for r in instance.rotations},
    )
    members = [
        (
            SimpleNamespace(pgy=resident.pgy, key=r.id, rotation_id=r.id),
            Weekday.MONDAY,
            Session.MORNING,
            literal,
            None,
        )
        for r, literal in [
            (
                clinic,
                _Conditional(
                    clinic_present,
                    session_selected,
                    pick=1,
                    domain_size=2,
                    negated=False,
                ),
            ),
            (standard, standard_present),
        ]
    ]
    groups = _materialize_week_entries(
        context,
        1,
        {(resident.id, Weekday.MONDAY, Session.MORNING, None): members},
        SimpleNamespace(in_clinic=defaultdict(list)),
    )
    _add_half_day_capacity(context, 1, groups[4])
    solver = cp_model.CpSolver()
    assert solver.Solve(model) == cp_model.INFEASIBLE


def test_september_14_non_clinic_rotations_need_four_attendings():
    """Three first-years at two points plus seven seniors use thirteen points."""
    from rbs.ui.clinic.projection import occupancy, site_capacity_points

    raw = sample_instance().model_dump(mode="json")
    for rotation in raw["rotations"]:
        if rotation["kind"] == "clinic":
            for rule in rotation["pgy_rules"]:
                rule["capacity_per_resident"] = 2 if rule["pgy"] == 1 else 1
    instance = SchedulerInput.model_validate(raw)
    people = [r for r in instance.residents if r.pgy == 1 and 12 not in r.vacation_weeks][:3]
    people += [r for r in instance.residents if r.pgy != 1 and 12 not in r.vacation_weeks][:7]
    rotation = next(r for r in instance.rotations if r.kind == RotationKind.STANDARD)
    assignments = [
        Assignment(
            resident_id=resident.id,
            rotation_id=rotation.id,
            kind=rotation.kind,
            start_week=12,
            end_week=12,
            weeks=[12],
            clinic_slots=[
                AssignedClinic(
                    week=12,
                    weekday=Weekday.MONDAY,
                    session=Session.AFTERNOON,
                    site="cedar",
                )
            ],
        )
        for resident in people
    ]
    schedule = Schedule(
        meta=dict(academic_year=instance.academic_year, engine="stub", status="feasible"),
        assignments=assignments,
    )
    board = occupancy(instance, schedule)
    assert site_capacity_points(board[12, Weekday.MONDAY, Session.AFTERNOON], "cedar") == 13
    assert attending_load(instance, schedule, site="cedar") == (4, 4)
    assert clinic_weekly_attendings(instance, assignments, "cedar") == {12: 4}
    assert not clinic_capacity_violations(instance, schedule)
