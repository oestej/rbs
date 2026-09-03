"""Configurable, residency-managed Clinic blocks."""

from collections.abc import Iterable

from rbs.models.clinic import ClinicPolicy, ClinicSlot
from rbs.models.enums import RotationKind
from rbs.models.instance import SolverProblem
from rbs.models.rotation import Rotation
from rbs.models.schedule import AssignedClinic
from rbs.solver.core.context import ClinicDecision, PlanningContext, new_clinic_decision

NOTES = [
    "clinic kind: enabled sessions are Clinic except configured Admin and Academic time",
]


def admin_domain(
    rotation: Rotation,
    policy: ClinicPolicy,
    *,
    extra_academic: Iterable[tuple[object, object]] = (),
) -> list[ClinicSlot]:
    blocked = {
        (policy.academic.weekday, policy.academic.session),
        *extra_academic,
    }
    rule = rotation.clinic
    if rule is None:
        return []
    return [
        slot
        for slot in rule.expanded_slots()
        if slot.weekday is not None
        and slot.session is not None
        and (slot.weekday, slot.session) not in blocked
    ]


def week_domain(
    instance: SolverProblem,
    week: int,
    rotation: Rotation | None = None,
) -> list[ClinicSlot]:
    """Enabled Clinic-block sessions after that week's Academic override."""
    rotations = (
        [rotation]
        if rotation is not None
        else [
            item
            for item in instance.rotations
            if item.kind is RotationKind.CLINIC
        ]
    )
    by_time: dict[tuple, ClinicSlot] = {}
    for item in rotations:
        if item.clinic is None or item.clinic_hours_disabled:
            continue
        for slot in item.clinic.expanded_slots():
            if slot.weekday is None or slot.session is None:
                continue
            if instance.is_academic_half_day(week, slot.weekday, slot.session):
                continue
            by_time.setdefault((slot.weekday, slot.session), slot)
    return list(by_time.values())


def constraints(context: PlanningContext) -> dict[str, ClinicDecision]:
    """Select each Clinic block's configured number of Admin sessions."""
    chosen: dict[str, ClinicDecision] = {}
    for occ in context.occurrences:
        rotation = context.rotations[occ.rotation_id]
        if rotation.kind is not RotationKind.CLINIC or rotation.clinic_hours_disabled:
            continue
        rule = rotation.clinic
        if rule is None or rule.admin_half_days_per_week <= 0:
            continue
        domain = admin_domain(
            rotation,
            context.instance.clinic_policy,
            extra_academic=(
                (override.weekday, override.session)
                for override in context.instance.academic_half_day_overrides
            ),
        )
        if not domain:
            continue
        chosen[occ.key] = new_clinic_decision(
            context.model,
            f"admin:{occ.key}",
            domain,
            pick=rule.admin_half_days_per_week,
        )
    return chosen


def clinic_slots(
    rotation: Rotation, solved: list[AssignedClinic] | None = None
) -> list[AssignedClinic]:
    """Return configured administrative sessions for the dedicated block."""
    _ = rotation
    if not solved:
        return []
    return [
        AssignedClinic(
            weekday=slot.weekday,
            session=slot.session,
            site=slot.site,
            allowed_sites=slot.allowed_sites,
            admin=True,
        )
        for slot in solved
    ]
