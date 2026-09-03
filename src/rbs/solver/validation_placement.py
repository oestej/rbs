"""Schedule validation: placement rules, shapes, and clinic minimums."""

from __future__ import annotations

from collections import defaultdict
from math import ceil, floor

from rbs.models.clinic import clinic_slot_date
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SolverProblem
from rbs.models.schedule import Schedule


def _validate_placement_rules(
    instance: SolverProblem,
    grid: dict[str, dict[str, str]],
    residents: dict,
    errors: list[str],
    *,
    successful: bool,
) -> None:
    for resident_id, weeks in grid.items():
        resident = residents.get(resident_id)
        if resident is None:
            continue
        runs = _rotation_runs(weeks, instance.calendar.weeks)
        for rotation in instance.rotations:
            try:
                rule = rotation.pgy_rule(resident.pgy)
            except KeyError:
                continue
            target_runs = runs.get(rotation.id, [])
            if not target_runs:
                continue
            if rule.earliest_start_week is not None:
                for start, _end in target_runs:
                    if start < rule.earliest_start_week:
                        errors.append(
                            f"{resident_id} {rotation.id} starts in week {start}, before "
                            f"{instance.training_level_label(resident.pgy, compact=True)} "
                            "earliest block "
                            f"(week {rule.earliest_start_week})"
                        )
            for predecessor_id in rule.prerequisite_rotation_ids:
                if not successful:
                    continue
                predecessor_runs = runs.get(predecessor_id, [])
                for start, _end in target_runs:
                    if not any(
                        predecessor_end < start
                        for _predecessor_start, predecessor_end in predecessor_runs
                    ):
                        errors.append(
                            f"{resident_id} {rotation.id} starts in week {start} before a "
                            f"completed {predecessor_id} block"
                        )


def _rotation_runs(
    weeks: dict[str, str],
    calendar_weeks: int,
) -> dict[str, list[tuple[int, int]]]:
    runs: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current_rotation: str | None = None
    start = 0
    end = 0
    for week in range(1, calendar_weeks + 1):
        rotation_id = weeks.get(str(week))
        if rotation_id == current_rotation:
            end = week
            continue
        if current_rotation is not None:
            runs[current_rotation].append((start, end))
        current_rotation = rotation_id
        start = week
        end = week
    if current_rotation is not None:
        runs[current_rotation].append((start, end))
    return runs


def _validate_consecutive(
    instance: SolverProblem, grid: dict[str, dict[str, str]], errors: list[str]
) -> None:
    for resident_id, weeks in grid.items():
        for rotation in instance.rotations:
            limit = rotation.max_consecutive_weeks
            if limit is None:
                continue
            run = 0
            for week in range(1, instance.calendar.weeks + 1):
                if weeks.get(str(week)) == rotation.id:
                    run += 1
                    if run > limit:
                        errors.append(
                            f"{resident_id} has more than {limit} consecutive weeks on "
                            f"{rotation.id}"
                        )
                        break
                else:
                    run = 0


def _validate_total_weeks(
    instance: SolverProblem, grid: dict[str, dict[str, str]], errors: list[str]
) -> None:
    for resident_id, weeks in grid.items():
        resident = instance.residents_by_id.get(resident_id)
        if resident is None:
            continue
        totals: dict[str, int] = defaultdict(int)
        for rotation_id in weeks.values():
            totals[rotation_id] += 1
        for rotation in instance.rotations:
            try:
                pgy_limit = rotation.pgy_rule(resident.pgy).max_total_weeks
                limit = rotation.max_total_weeks_for_pgy(resident.pgy)
            except KeyError:
                continue
            if limit is None or totals[rotation.id] <= limit:
                continue
            scope = (
                f"{instance.training_level_label(resident.pgy, compact=True)} "
                if pgy_limit is not None
                and (rotation.max_total_weeks is None or pgy_limit < rotation.max_total_weeks)
                else ""
            )
            errors.append(
                f"{resident_id} has {totals[rotation.id]} total weeks on {rotation.id}, "
                f"exceeding its {scope}{limit}-week maximum"
            )


def _validate_clinics(
    instance: SolverProblem,
    schedule: Schedule,
    errors: list[str],
    *,
    successful: bool,
) -> None:
    policy = instance.clinic_policy
    has_manual_overrides = any(
        assignment.manual_clinic_baselines
        or any(slot.manual_override for slot in assignment.clinic_slots)
        for assignment in schedule.assignments
    )
    enforce_planning_targets = successful and not has_manual_overrides
    filled: dict[tuple[str, int, object, object], int] = defaultdict(int)
    inpatient_concurrent: dict[tuple[str, int, object, object], set[str]] = defaultdict(set)
    by_resident: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for assignment in schedule.assignments:
        rotation = instance.rotations_by_id.get(assignment.rotation_id)
        for slot in assignment.clinic_slots:
            weeks = [slot.week] if slot.week is not None else assignment.weeks
            if (
                rotation is not None
                and rotation.kind is RotationKind.FMED
                and rotation.clinic is not None
                and not slot.admin
                and not slot.manual_override
            ):
                for week in weeks:
                    inpatient_concurrent[
                        assignment.rotation_id,
                        week,
                        slot.weekday,
                        slot.session,
                    ].add(assignment.resident_id)
            if slot.admin or slot.site is None or slot.site not in policy.site_ids:
                continue
            for week in weeks:
                clinic_name = policy.site_name(slot.site)
                calendar_day = clinic_slot_date(
                    instance.calendar.first_week_start,
                    week,
                    slot.weekday,
                )
                maximum = policy.max_capacity_on(
                    slot.site,
                    calendar_day,
                    slot.session,
                )
                if maximum <= 0:
                    errors.append(
                        f"{clinic_name} clinic has no attending coverage: week {week} "
                        f"{slot.weekday.value} {slot.session.value}"
                    )
                key = (slot.site, week, slot.weekday, slot.session)
                filled[key] += 1
                by_resident[assignment.resident_id][slot.site] += 1
                if maximum > 0 and filled[key] > maximum:
                    errors.append(
                        f"{clinic_name} capacity exceeded: week {week} "
                        f"{slot.weekday.value} {slot.session.value} "
                        f"({filled[key]} residents; max {maximum})"
                    )

    for (rotation_id, week, weekday, session), resident_ids in inpatient_concurrent.items():
        rotation = instance.rotations_by_id[rotation_id]
        rule = rotation.clinic
        assert rule is not None
        if rule.max_concurrent is not None and len(resident_ids) > rule.max_concurrent:
            errors.append(
                f"{rotation.code} clinic concurrency exceeded: week {week} "
                f"{weekday.value} {session.value} "
                f"({len(resident_ids)} residents; max {rule.max_concurrent})"
            )
        residents_by_pgy: dict[int, int] = defaultdict(int)
        for resident_id in resident_ids:
            resident = instance.residents_by_id.get(resident_id)
            if resident is not None:
                residents_by_pgy[resident.pgy] += 1
        for pgy, maximum in rule.max_concurrent_by_pgy.items():
            if residents_by_pgy[pgy] > maximum:
                errors.append(
                    f"{rotation.code} "
                    f"{instance.training_level_label(pgy, compact=True)} "
                    f"clinic concurrency exceeded: week {week} "
                    f"{weekday.value} {session.value} "
                    f"({residents_by_pgy[pgy]} residents; max {maximum})"
                )

    for clinic in policy.sites:
        for week in range(1, instance.calendar.weeks + 1):
            for weekday in Weekday:
                calendar_day = clinic_slot_date(
                    instance.calendar.first_week_start,
                    week,
                    weekday,
                )
                for session in Session:
                    minimum = policy.min_capacity_on(
                        clinic.id,
                        calendar_day,
                        session,
                    )
                    if (
                        not enforce_planning_targets
                        or minimum <= 0
                        or instance.is_academic_half_day(
                            week,
                            weekday,
                            session,
                        )
                    ):
                        continue
                    count = filled[
                        clinic.id,
                        week,
                        weekday,
                        session,
                    ]
                    if count < minimum:
                        errors.append(
                            f"{clinic.name} below minimum capacity: week {week} "
                            f"{weekday.value} {session.value} "
                            f"({count} residents; min {minimum})"
                        )

    for resident_id, counts in by_resident.items():
        if not enforce_planning_targets:
            continue
        total = sum(counts.values())
        if total <= 0:
            continue
        resident = next(item for item in instance.residents if item.id == resident_id)
        for rule in policy.allocation_rules_for(
            pgy=resident.pgy,
            resident_id=resident_id,
        ):
            count = counts.get(rule.clinic_id, 0)
            minimum = ceil(rule.min_fraction * total - 1e-9)
            maximum = floor(rule.max_fraction * total + 1e-9)
            if count < minimum:
                errors.append(
                    f"{resident_id} has {count}/{total} clinic sessions at "
                    f"{policy.site_name(rule.clinic_id)}; minimum is {minimum}"
                )
            if count > maximum:
                errors.append(
                    f"{resident_id} has {count}/{total} clinic sessions at "
                    f"{policy.site_name(rule.clinic_id)}; maximum is {maximum}"
                )
