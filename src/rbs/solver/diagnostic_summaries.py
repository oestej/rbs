"""Compact structured explanations for invalid post-solve schedules."""

from __future__ import annotations

from collections import defaultdict

from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule, SolverDiagnostic
from rbs.solver.validation_placement import (
    ClinicCapacityViolation,
    clinic_capacity_violations,
)


def validation_failure_diagnostics(
    instance: SolverProblem,
    schedule: Schedule,
    errors: tuple[str, ...] | list[str],
) -> list[SolverDiagnostic]:
    """Group repetitive validation failures while retaining every distinct cause."""
    violations = clinic_capacity_violations(instance, schedule)
    by_clinic: dict[str, list[ClinicCapacityViolation]] = defaultdict(list)
    for violation in violations:
        by_clinic[violation.clinic_id].append(violation)

    diagnostics: list[SolverDiagnostic] = []
    for clinic_id in instance.clinic_policy.site_ids:
        clinic_violations = by_clinic.get(clinic_id, [])
        if not clinic_violations:
            continue
        clinic_name = instance.clinic_policy.site_name(clinic_id)
        uncovered = [item for item in clinic_violations if item.maximum <= 0]
        over_capacity = [item for item in clinic_violations if item.maximum > 0]
        details: list[str] = []
        if over_capacity:
            peak = max(
                over_capacity,
                key=lambda item: (
                    item.resident_count - item.maximum,
                    item.resident_count,
                ),
            )
            count = len(over_capacity)
            details.append(
                f"{count} clinic {'half-day exceeds' if count == 1 else 'half-days exceed'} "
                f"capacity; the largest overflow is week {peak.week} "
                f"{peak.weekday.value} {peak.session.value} "
                f"({peak.resident_count} residents; max {peak.maximum})"
            )
        if uncovered:
            count = len(uncovered)
            details.append(
                f"{count} scheduled clinic {'half-day has' if count == 1 else 'half-days have'} "
                "no attending coverage"
            )
        suggestions = [
            f"Increase {clinic_name} attending coverage on the affected half-days.",
            "Move eligible clinic sessions to another open site or half-day.",
        ]
        if schedule.meta.metrics.elective_fallback_blocks:
            suggestions.append(
                "Configure resident Elective preferences to reduce Clinic fallback blocks."
            )
        diagnostics.append(
            SolverDiagnostic(
                code="clinic_allocation_capacity",
                message=f"{clinic_name}: " + "; ".join(details) + ".",
                weeks=sorted({item.week for item in clinic_violations}),
                suggestions=suggestions,
            )
        )

    capacity_messages = {violation.message for violation in violations}
    remaining = [error for error in dict.fromkeys(errors) if error not in capacity_messages]
    diagnostics.extend(
        SolverDiagnostic(code="schedule_validation", message=error)
        for error in remaining
    )
    return diagnostics
