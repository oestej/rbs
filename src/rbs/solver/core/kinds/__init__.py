"""Kind-specific rotation rules.

The engine dispatches on ``Rotation.kind``, never on rotation id. Add a module
here when a new kind needs custom constraints or decode behavior.
"""

from rbs.models.clinic import ClinicPolicy, ClinicRule
from rbs.models.enums import RotationKind
from rbs.models.rotation import Rotation
from rbs.models.schedule import AssignedClinic
from rbs.solver.core.context import ClinicDecision, PlanningContext

from . import clinic as clinic_kind
from . import fmed as fmed_kind


def apply_constraints(context: PlanningContext) -> dict[str, ClinicDecision]:
    """Compile kind-specific choices into typed clinic decisions."""
    extras: dict[str, ClinicDecision] = {}
    extras.update(fmed_kind.unique_clinic(context))
    extras.update(clinic_kind.constraints(context))
    return extras


def clinic_slots_for(
    rotation: Rotation,
    solved: list[AssignedClinic] | None,
    policy: ClinicPolicy | None = None,
) -> list[AssignedClinic]:
    if rotation.clinic_hours_disabled:
        return []
    if rotation.kind is RotationKind.CLINIC:
        return clinic_kind.clinic_slots(rotation, solved)
    if rotation.kind is RotationKind.FMED:
        return fmed_kind.clinic_slots(rotation, solved)
    if solved:
        return solved
    return overlay_slots(rotation.clinic, policy)


def overlay_slots(
    rule: ClinicRule | None,
    policy: ClinicPolicy | None = None,
) -> list[AssignedClinic]:
    """Single continuity-clinic half-day overlaid on a host rotation."""
    if rule is None or rule.half_days_per_week == 0:
        return []
    chosen: list[AssignedClinic] = []
    for slot in rule.expanded_slots():
        if slot.weekday is None or slot.session is None:
            continue
        if policy is not None and policy.is_academic(slot):
            continue
        chosen.append(
            AssignedClinic(
                weekday=slot.weekday,
                session=slot.session,
                allowed_sites=slot.sites,
            )
        )
        if len(chosen) >= rule.half_days_per_week:
            break
    return chosen


def notes() -> list[str]:
    return [
        *fmed_kind.NOTES,
        *clinic_kind.NOTES,
    ]
