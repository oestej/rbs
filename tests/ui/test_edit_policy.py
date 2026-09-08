from datetime import timedelta

from rbs.catalog import sample_instance
from rbs.ui.edit_policy import instance_edit_impact
from rbs.workspaces import InstanceEditImpact


def test_display_labels_do_not_make_a_schedule_stale() -> None:
    instance = sample_instance()

    residents = list(instance.residents)
    residents[0] = residents[0].revised(name="Renamed resident")

    rotations = list(instance.rotations)
    rotations[0] = rotations[0].revised(name="Renamed rotation", code="RENAM")

    requirements = list(instance.requirements)
    requirements[0] = requirements[0].revised(code="YR1", label="First year")

    special_rotations = list(instance.special_rotations)
    special_rotations[0] = special_rotations[0].revised(name="Renamed event")

    sites = list(instance.clinic_policy.sites)
    site_closures = list(sites[0].closure_days)
    site_closures[0] = site_closures[0].revised(name="Renamed site closure")
    sites[0] = sites[0].revised(
        name="Renamed clinic",
        closure_days=site_closures,
    )
    shared_closures = list(instance.clinic_policy.closure_days)
    shared_closures[0] = shared_closures[0].revised(name="Renamed closure")
    clinic_policy = instance.clinic_policy.revised(
        sites=sites,
        closure_days=shared_closures,
    )

    presentation_edits = [
        instance.revised(residents=residents),
        instance.revised(rotations=rotations),
        instance.revised(requirements=requirements),
        instance.revised(special_rotations=special_rotations),
        instance.revised(clinic_policy=clinic_policy),
    ]

    assert all(
        instance_edit_impact(instance, edited) is InstanceEditImpact.PRESENTATION
        for edited in presentation_edits
    )


def test_solver_preferences_and_problem_edits_have_distinct_impacts() -> None:
    instance = sample_instance()
    solver = instance.solver.revised(
        time_limit_seconds=instance.solver.time_limit_seconds + 1
    )
    resident = instance.residents[0]
    residents = list(instance.residents)
    residents[0] = resident.revised(
        days_off=[
            *resident.days_off,
            instance.calendar.first_week_start + timedelta(days=1),
        ]
    )

    assert (
        instance_edit_impact(instance, instance.revised(solver=solver))
        is InstanceEditImpact.APPLICATION_PREFERENCE
    )
    assert (
        instance_edit_impact(instance, instance.revised(residents=residents))
        is InstanceEditImpact.SOLVER_INPUT
    )
