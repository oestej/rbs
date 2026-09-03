"""Human-readable CLI summary of a scheduling instance.

This is presentation for ``rbs validate``, not validation logic: integrity
checking lives on the models (``SolverProblem.check_integrity``) and in
``rbs.solver.validation``.
"""

from rbs.models.instance import SchedulerInput


def summarize(instance: SchedulerInput) -> str:
    counts = instance.cohort_counts()
    lines = [
        f"academic_year: {instance.academic_year}",
        (
            f"weeks: {instance.calendar.weeks} "
            f"(start {instance.calendar.first_week_start.isoformat()})"
        ),
        "residents: "
        + " ".join(
            f"{curriculum.short_code}={counts.get(curriculum.pgy, 0)}"
            for curriculum in instance.requirements
        )
        + f" (total {len(instance.residents)})",
        f"rotations: {len(instance.rotations)}",
    ]
    for curriculum in instance.requirements:
        lines.append(
            f"{curriculum.short_code} curriculum: "
            f"{len(curriculum.blocks)} required block types, "
            f"{curriculum.required_weeks()} weeks"
        )
    vac_counts = [len(resident.vacation_weeks) for resident in instance.residents]
    if vac_counts:
        lines.append(f"vacation weeks per resident: min={min(vac_counts)} max={max(vac_counts)}")
    day_off_counts = [len(resident.days_off) for resident in instance.residents]
    if day_off_counts:
        lines.append(
            "individual days off per resident: "
            f"min={min(day_off_counts)} max={max(day_off_counts)}"
        )
    locked_weeks = sum(len(lock.weeks) for lock in instance.locks)
    lines.append(f"locks: {len(instance.locks)} ({locked_weeks} resident-weeks pinned)")
    return "\n".join(lines)
