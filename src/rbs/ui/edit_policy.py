"""Application policy for how accepted edits affect the current schedule."""

from __future__ import annotations

from typing import Any

from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.workspaces import InstanceEditImpact


def instance_edit_impact(
    previous: SchedulerInput,
    replacement: SchedulerInput,
) -> InstanceEditImpact:
    """Classify a generic edit by the scheduling semantics it changes.

    The child-process projection intentionally retains names and codes for
    diagnostics. They do not alter feasibility or objectives, so this policy
    removes those display labels before comparing solver inputs.
    """
    if _solver_semantics(previous) != _solver_semantics(replacement):
        return InstanceEditImpact.SOLVER_INPUT
    if previous.solver != replacement.solver:
        return InstanceEditImpact.APPLICATION_PREFERENCE
    return InstanceEditImpact.PRESENTATION


def _solver_semantics(instance: SchedulerInput) -> dict[str, Any]:
    payload = SolverProblem.from_instance(
        instance,
        today=instance.calendar.first_week_start,
    ).model_dump(mode="json")
    for resident in payload["residents"]:
        resident.pop("name", None)
    for rotation in payload["rotations"]:
        rotation.pop("code", None)
        rotation.pop("name", None)
    for curriculum in payload["requirements"]:
        curriculum.pop("code", None)
        curriculum.pop("label", None)
    for special in payload["special_rotations"]:
        special.pop("name", None)
    payload["special_rotations"].sort(key=lambda special: special["id"])
    policy = payload["clinic_policy"]
    for site in policy["sites"]:
        site.pop("name", None)
        for closure in site["closure_days"]:
            closure.pop("name", None)
    for closure in policy["closure_days"]:
        closure.pop("name", None)
    return payload
