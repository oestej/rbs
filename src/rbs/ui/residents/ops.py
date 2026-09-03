"""Resident mutations and schedule-report helpers, free of NiceGUI."""

from __future__ import annotations

import calendar as calendar_module
from datetime import date, timedelta

from rbs.clinic_locks import (
    clinic_slot_is_in_automatic_lock_window,
    clinic_slot_is_locked,
)
from rbs.models.clinic import clinic_slot_date
from rbs.models.color_scheme import contrasting_text_color
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.models.schedule import AssignedClinic, Assignment, Schedule
from rbs.models.special import SpecialRotationKind
from rbs.solver.validation import validate_schedule_or_raise
from rbs.ui.clinic.board import (
    ClinicOccupant,
    clinic_kind_slots_for_week,
    clinic_weekdays,
    occupancy,
    occupant_site,
    site_headcount,
)
from rbs.ui.grid import rotation_color_class, visible_week_numbers
from rbs.ui.schedule_styles import SPECIAL_EVENT_COLOR, SPECIAL_EVENT_TINT

ClinicOccupancy = dict[tuple[int, Weekday, Session], list[ClinicOccupant]]


def replace_resident(
    instance: SchedulerInput,
    original_id: str,
    replacement: Resident,
) -> SchedulerInput:
    """Replace one resident and keep resident-referencing locks synchronized."""
    if not any(resident.id == original_id for resident in instance.residents):
        raise ValueError(f"unknown resident {original_id!r}")

    residents = [
        replacement if resident.id == original_id else resident for resident in instance.residents
    ]
    locks = [
        lock.model_copy(update={"resident_id": replacement.id})
        if lock.resident_id == original_id
        else lock
        for lock in instance.locks
    ]
    return instance.revised(residents=residents, locks=locks)


def add_resident(instance: SchedulerInput, resident: Resident) -> SchedulerInput:
    return instance.revised(residents=[*instance.residents, resident])


def next_resident_id(instance: SchedulerInput) -> str:
    """Return a neutral, stable system ID without encoding the training level."""
    used = {resident.id for resident in instance.residents}
    sequence = 1
    while True:
        candidate = f"resident-{sequence:03d}"
        if candidate not in used:
            return candidate
        sequence += 1


def vacation_week_for_monday(instance: SchedulerInput, value: str) -> int:
    selected = _parse_vacation_monday(value)
    delta = (selected - instance.calendar.first_week_start).days
    week = delta // 7 + 1
    if delta < 0 or delta % 7 or week > instance.calendar.weeks:
        start = instance.calendar.first_week_start
        end = start + timedelta(weeks=instance.calendar.weeks - 1)
        raise ValueError(f"vacation Monday must be between {start:%b %d, %Y} and {end:%b %d, %Y}")
    return week


def vacation_range_for_monday(value: str) -> dict[str, str]:
    """Return the Monday-through-Sunday range highlighted by the vacation picker."""
    selected = _parse_vacation_monday(value)
    return {
        "from": selected.isoformat(),
        "to": (selected + timedelta(days=6)).isoformat(),
    }


def vacation_month_dates(year: int, month: int) -> list[date]:
    """Return a complete Sunday-first calendar grid for one month."""
    weeks = calendar_module.Calendar(firstweekday=calendar_module.SUNDAY).monthdatescalendar(
        year, month
    )
    return [day for week in weeks for day in week]


def _parse_vacation_monday(value: str) -> date:
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("select a vacation Monday from the calendar") from exc
    if selected.weekday() != 0:
        raise ValueError("vacation dates must be Mondays")
    return selected


def vacation_monday(instance: SchedulerInput, week: int) -> date:
    return instance.calendar.first_week_start + timedelta(weeks=week - 1)


def vacation_monday_is_selectable(instance: SchedulerInput, day: date) -> bool:
    """Return whether ``day`` is one of the academic year's Monday anchors."""
    return (
        day.weekday() == calendar_module.MONDAY
        and instance.calendar.first_week_start
        <= day
        <= vacation_monday(instance, instance.calendar.weeks)
    )


def day_off_date(instance: SchedulerInput, value: str) -> date:
    """Parse one individual day off and require it to be in the academic year."""
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("select a day off from the calendar") from exc
    if not day_off_is_selectable(instance, selected):
        first_day = instance.calendar.first_week_start
        last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
        raise ValueError(f"day off must be between {first_day:%b %d, %Y} and {last_day:%b %d, %Y}")
    return selected


def day_off_is_selectable(instance: SchedulerInput, day: date) -> bool:
    """Return whether ``day`` is inside the complete 52-week academic year."""
    first_day = instance.calendar.first_week_start
    last_day = first_day + timedelta(days=instance.calendar.weeks * 7 - 1)
    return first_day <= day <= last_day


def resident_schedule_report_rows(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
    *,
    show_completed: bool = True,
    today: date | None = None,
) -> list[dict[str, str]]:
    """Return chronological rotation and vacation line items for one resident."""
    resident = next(
        (item for item in instance.residents if item.id == resident_id),
        None,
    )
    if resident is None:
        raise ValueError(f"unknown resident {resident_id!r}")
    assignments = sorted(
        (
            assignment
            for assignment in (schedule.assignments if schedule is not None else [])
            if assignment.resident_id == resident_id
        ),
        key=lambda assignment: (
            assignment.start_week,
            assignment.end_week,
            assignment.rotation_id,
        ),
    )
    visible_weeks = set(
        visible_week_numbers(
            instance.calendar.first_week_start,
            instance.calendar.weeks,
            show_past_weeks=show_completed,
            today=today,
        )
    )
    rows: list[dict[str, str]] = []
    conferences = instance.special_rotations_for_resident(
        resident_id,
        kind=SpecialRotationKind.CONFERENCE,
    )
    for index, assignment in enumerate(assignments):
        rotation = instance.rotation(assignment.rotation_id)
        rotation_name = instance.assignment_name(
            assignment.rotation_id,
            elective=assignment.elective,
        )
        rotation_color = instance.assignment_color(
            assignment.rotation_id,
            elective=assignment.elective,
        )
        vacation_weeks = set(assignment.weeks) & set(resident.vacation_weeks)
        rotation_seen = False
        for segment_index, (segment, segment_kind) in enumerate(
            _assignment_schedule_segments(assignment.weeks, vacation_weeks)
        ):
            segment_start = vacation_monday(instance, segment[0])
            segment_end = vacation_monday(instance, segment[-1]) + timedelta(days=6)
            date_segments: list[tuple[date, date, bool]] = []
            if segment_kind == "vacation":
                date_segments.append((segment_start, segment_end, False))
            else:
                cursor = segment_start
                for special in conferences:
                    overlap_start = max(cursor, special.start_date)
                    overlap_end = min(segment_end, special.end_date)
                    if overlap_start > overlap_end:
                        continue
                    if cursor < overlap_start:
                        date_segments.append(
                            (
                                cursor,
                                overlap_start - timedelta(days=1),
                                rotation_seen,
                            )
                        )
                        rotation_seen = True
                    cursor = overlap_end + timedelta(days=1)
                    if cursor > segment_end:
                        break
                if cursor <= segment_end:
                    date_segments.append((cursor, segment_end, rotation_seen))
                    rotation_seen = True

            for part_index, (part_start, part_end, continuation) in enumerate(date_segments):
                visible_part = _visible_assignment_date_segment(
                    instance,
                    part_start,
                    part_end,
                    visible_weeks,
                )
                if visible_part is None:
                    continue
                segment_weeks, visible_start, visible_end = visible_part
                week_range = _compact_week_ranges(segment_weeks)
                week_noun = "Week" if len(segment_weeks) == 1 else "Weeks"
                date_range = _vacation_date_range(visible_start, visible_end)
                period = f"{week_noun} {week_range} ({date_range})"

                if segment_kind == "vacation":
                    rotation_text = "Vacation"
                    days_off = ""
                else:
                    rotation_heading = (
                        f"{rotation.code} · {rotation_name} (Cont.)"
                        if continuation
                        else f"{rotation.code} · {rotation_name}"
                    )
                    individual_days = [
                        selected_day
                        for selected_day in resident.days_off
                        if visible_start <= selected_day <= visible_end
                    ]
                    days_off = _individual_days_off_label(individual_days)
                    details = [detail for detail in (days_off,) if detail]
                    rotation_text = "\n".join([rotation_heading, *details])

                rows.append(
                    {
                        "_key": (
                            f"{assignment.start_week}:{assignment.end_week}:"
                            f"{assignment.rotation_id}:"
                            f"{int(assignment.elective)}:{index}:{segment_index}:{part_index}"
                        ),
                        "period": period,
                        "weeks": week_range,
                        "dates": date_range,
                        "kind": segment_kind,
                        "rotation": rotation_text,
                        "rotation_code": rotation.code,
                        "rotation_name": rotation_name,
                        "color": rotation_color,
                        "foreground": contrasting_text_color(rotation_color),
                        "color_class": rotation_color_class(rotation_color),
                        "continuation": "true" if continuation else "false",
                        "days_off": days_off,
                        "_sort_date": visible_start.isoformat(),
                        "_sort_end": visible_end.isoformat(),
                        "_sort_order": "1",
                        "_start_week": str(segment_weeks[0]),
                        "_end_week": str(segment_weeks[-1]),
                    }
                )

    for special in conferences:
        all_special_weeks = {
            (calendar_day - instance.calendar.first_week_start).days // 7 + 1
            for calendar_day in special.dates()
        }
        special_weeks = sorted(all_special_weeks & visible_weeks)
        if not special_weeks:
            continue
        week_range = _compact_week_ranges(special_weeks)
        week_noun = "Week" if len(special_weeks) == 1 else "Weeks"
        date_range = _vacation_date_range(special.start_date, special.end_date)
        rows.append(
            {
                "_key": f"special:{special.id}",
                "period": f"{week_noun} {week_range} ({date_range})",
                "weeks": week_range,
                "dates": date_range,
                "kind": "special",
                "rotation": f"Conference/Multi-Day\n{special.name}",
                "rotation_code": "CONF",
                "rotation_name": special.name,
                "color": "",
                "color_class": "rbs-special-rotation-color",
                "continuation": "false",
                "days_off": "",
                "_sort_date": special.start_date.isoformat(),
                "_sort_end": special.end_date.isoformat(),
                "_sort_order": "0",
                "_start_week": str(special_weeks[0]),
                "_end_week": str(special_weeks[-1]),
            }
        )

    rows.sort(
        key=lambda row: (
            row["_sort_date"],
            int(row["_sort_order"]),
            row["_key"],
        )
    )
    rows = _coalesce_adjacent_vacation_rows(rows)
    for row in rows:
        row.pop("_sort_date", None)
        row.pop("_sort_end", None)
        row.pop("_sort_order", None)
        row.pop("_start_week", None)
        row.pop("_end_week", None)
    return rows


def _coalesce_adjacent_vacation_rows(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Join uninterrupted vacation lanes split by an assignment boundary."""
    coalesced: list[dict[str, str]] = []
    for row in rows:
        previous = coalesced[-1] if coalesced else None
        if (
            previous is not None
            and previous["kind"] == "vacation"
            and row["kind"] == "vacation"
            and int(previous["_end_week"]) + 1 == int(row["_start_week"])
            and date.fromisoformat(previous["_sort_end"]) + timedelta(days=1)
            == date.fromisoformat(row["_sort_date"])
        ):
            start_week = int(previous["_start_week"])
            end_week = int(row["_end_week"])
            start = date.fromisoformat(previous["_sort_date"])
            end = date.fromisoformat(row["_sort_end"])
            week_range = _compact_week_ranges(range(start_week, end_week + 1))
            week_noun = "Week" if start_week == end_week else "Weeks"
            date_range = _vacation_date_range(start, end)
            previous.update(
                {
                    "_key": f"{previous['_key']}+{row['_key']}",
                    "_sort_end": row["_sort_end"],
                    "_end_week": row["_end_week"],
                    "period": f"{week_noun} {week_range} ({date_range})",
                    "weeks": week_range,
                    "dates": date_range,
                }
            )
            continue
        coalesced.append(row)
    return coalesced


def _assignment_schedule_segments(
    weeks: list[int],
    vacation_weeks: set[int],
) -> list[tuple[list[int], str]]:
    """Split an assignment around full-week vacation interruptions."""
    segments: list[tuple[list[int], str]] = []
    for week in weeks:
        if week in vacation_weeks:
            segment_kind = "vacation"
        else:
            segment_kind = "rotation"
        if segments and segments[-1][1] == segment_kind:
            segments[-1][0].append(week)
        else:
            segments.append(([week], segment_kind))
    return segments


def _visible_assignment_date_segment(
    instance: SchedulerInput,
    start: date,
    end: date,
    visible_weeks: set[int],
) -> tuple[list[int], date, date] | None:
    """Clip an exact-date assignment segment to the visible academic weeks."""
    first_day = instance.calendar.first_week_start
    start_week = (start - first_day).days // 7 + 1
    end_week = (end - first_day).days // 7 + 1
    weeks = [week for week in range(start_week, end_week + 1) if week in visible_weeks]
    if not weeks:
        return None
    visible_start = max(start, vacation_monday(instance, weeks[0]))
    visible_end = min(
        end,
        vacation_monday(instance, weeks[-1]) + timedelta(days=6),
    )
    return weeks, visible_start, visible_end


def _individual_days_off_label(days: list[date]) -> str:
    if not days:
        return ""
    noun = "Day" if len(days) == 1 else "Days"
    dates = ", ".join(f"{day:%b} {day.day}, {day:%Y}" for day in days)
    return f"{noun} off: {dates}"


def resident_clinic_schedule_report_rows(
    instance: SchedulerInput,
    schedule: Schedule | None,
    resident_id: str,
    *,
    show_completed: bool = True,
    today: date | None = None,
    clinic_occupancy: ClinicOccupancy | None = None,
) -> list[dict[str, str]]:
    """Return every visible academic week for one resident's clinic calendar."""
    resident = next(
        (item for item in instance.residents if item.id == resident_id),
        None,
    )
    if resident is None:
        raise ValueError(f"unknown resident {resident_id!r}")
    if schedule is None:
        return []

    resident_events = instance.special_rotations_for_resident(
        resident_id,
        kind=SpecialRotationKind.EVENT,
    )
    if schedule.is_empty() and not resident_events:
        return []

    visible_weeks = set(
        visible_week_numbers(
            instance.calendar.first_week_start,
            instance.calendar.weeks,
            show_past_weeks=show_completed,
            today=today,
        )
    )
    sessions_by_week: dict[
        int,
        dict[tuple[Weekday, Session], ClinicOccupant],
    ] = {week: {} for week in visible_weeks}
    special_events_by_week: dict[
        int,
        dict[tuple[Weekday, Session], object],
    ] = {}
    board = clinic_occupancy if clinic_occupancy is not None else occupancy(instance, schedule)
    for (week, weekday, session), people in board.items():
        if week not in visible_weeks:
            continue
        person = next((item for item in people if item.resident_id == resident_id), None)
        if person is None:
            continue
        sessions_by_week.setdefault(week, {})[(weekday, session)] = person

    for assignment in schedule.assignments:
        if assignment.resident_id != resident_id:
            continue
        for week in assignment.manual_clinic_baselines:
            if week in visible_weeks:
                sessions_by_week.setdefault(week, {})

    for special in resident_events:
        calendar_day = special.start_date
        week = (calendar_day - instance.calendar.first_week_start).days // 7 + 1
        if week not in visible_weeks:
            continue
        weekday = tuple(Weekday)[calendar_day.weekday()]
        sessions = tuple(Session) if special.session is None else (special.session,)
        for session in sessions:
            special_events_by_week.setdefault(week, {})[(weekday, session)] = special
        sessions_by_week.setdefault(week, {})

    resident_grid = schedule.week_grid.get(resident_id, {})
    rows: list[dict[str, str]] = []
    weekdays = clinic_weekdays(instance)
    for week, sessions in sorted(sessions_by_week.items()):
        monday = vacation_monday(instance, week)
        sunday = monday + timedelta(days=6)
        rotation_id = resident_grid.get(str(week))
        if rotation_id is None:
            rotation_label = "—"
        else:
            assignment = schedule.assignment_for(resident_id, week)
            rotation_label = instance.assignment_label(
                rotation_id,
                elective=bool(assignment and assignment.elective),
            )
        row = {
            "_key": str(week),
            "dates": _vacation_date_range(monday, sunday),
            "week": str(week),
            "rotation": rotation_label,
        }
        session_labels: list[str] = []
        for weekday in weekdays:
            calendar_day = monday + timedelta(days=list(Weekday).index(weekday))
            row[f"{weekday.value}_date"] = f"{calendar_day:%b} {calendar_day.day}"
            for session in Session:
                key = _resident_clinic_session_key(weekday, session)
                special = special_events_by_week.get(week, {}).get((weekday, session))
                if special is not None:
                    row[key] = special.name
                    row[f"{key}_kind"] = "special-event"
                    row[f"{key}_color"] = SPECIAL_EVENT_COLOR
                    row[f"{key}_tint"] = SPECIAL_EVENT_TINT
                    session_label = "AM" if session is Session.MORNING else "PM"
                    session_labels.append(
                        f"{weekday.value[:3].title()} {session_label} · {special.name}"
                    )
                    continue
                person = sessions.get((weekday, session))
                if person is None:
                    row[key] = ""
                    row[f"{key}_kind"] = ""
                    row[f"{key}_color"] = ""
                    row[f"{key}_tint"] = ""
                    continue
                location = "Admin"
                if not person.admin:
                    site = occupant_site(person)
                    location = person.site_name or (
                        instance.clinic_policy.site_name(site) if site is not None else "Clinic"
                    )
                row[key] = location
                row[f"{key}_kind"] = "admin" if person.admin else "site"
                row[f"{key}_color"] = person.site_color or ""
                row[f"{key}_tint"] = person.site_light_color or ""
                session_label = "AM" if session is Session.MORNING else "PM"
                session_labels.append(f"{weekday.value[:3].title()} {session_label} · {location}")
        row["sessions"] = "\n".join(session_labels)
        rows.append(row)
    return rows


def resident_clinic_slot_locked(
    schedule: Schedule,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    *,
    instance: SchedulerInput | None = None,
    today: date | None = None,
) -> bool:
    """Return the effective lock for one visible clinic occurrence."""
    if instance is not None:
        slot = resident_clinic_slot(
            instance,
            schedule,
            resident_id,
            week,
            weekday,
            session,
        )
        return clinic_slot_is_locked(instance, slot, week, today=today)
    assignment = _resident_assignment_for_week(schedule, resident_id, week)
    explicit = [
        slot
        for slot in assignment.clinic_slots
        if slot.week == week and slot.weekday is weekday and slot.session is session
    ]
    if explicit:
        return any(slot.locked for slot in explicit)
    if any(slot.week is not None for slot in assignment.clinic_slots):
        return False
    return any(
        slot.locked
        for slot in assignment.clinic_slots
        if slot.weekday is weekday and slot.session is session
    )


def resident_clinic_slot(
    instance: SchedulerInput,
    schedule: Schedule,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
) -> AssignedClinic:
    """Return the clinic occurrence shown in one resident-calendar half-day."""
    assignment = _resident_assignment_for_week(schedule, resident_id, week)
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    return slots[_resident_clinic_slot_index(slots, week, weekday, session)]


def resident_clinic_week_override_delta(
    instance: SchedulerInput,
    schedule: Schedule,
    resident_id: str,
    week: int,
) -> int:
    """Return extra (positive) or removed (negative) manual clinic sessions."""
    assignment = _resident_assignment_for_week(schedule, resident_id, week)
    baseline = assignment.manual_clinic_baselines.get(week)
    if baseline is None:
        return 0
    current = sum(
        slot.week == week for slot in _materialized_assignment_clinic_slots(instance, assignment)
    )
    return current - baseline


def resident_clinic_available_site_ids(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    clinic_occupancy: ClinicOccupancy | None = None,
) -> tuple[str, ...]:
    """Return sites with preceptor capacity for an added or reassigned block."""
    if instance.is_academic_half_day(week, weekday, session):
        return ()
    available: list[str] = []
    for site in instance.clinic_policy.sites:
        candidate = AssignedClinic(
            weekday=weekday,
            session=session,
            site=site.id,
            week=week,
        )
        if not _resident_clinic_preceptor_conflicts(
            instance,
            schedule,
            resident_id=resident_id,
            week=week,
            weekday=weekday,
            session=session,
            source_slot=candidate,
            clinic_occupancy=clinic_occupancy,
        ):
            available.append(site.id)
    return tuple(available)


def set_resident_clinic_slot_locked(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    locked: bool,
    today: date | None = None,
) -> Schedule:
    """Persist the drag lock for one clinic occurrence.

    Historical schedules can store one recurring slot for a whole assignment.
    Materializing those slots by week lets a single visible occurrence be locked
    without changing the others.
    """
    assignment_index, assignment = _resident_assignment_index(
        schedule,
        resident_id,
        week,
    )
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    slot_index = _resident_clinic_slot_index(slots, week, weekday, session)
    slot = slots[slot_index]
    effective_lock = clinic_slot_is_locked(instance, slot, week, today=today)
    if effective_lock is locked:
        return schedule
    automatic_lock_window = clinic_slot_is_in_automatic_lock_window(
        instance,
        slot,
        week,
        today=today,
    )
    slots[slot_index] = slot.model_copy(
        update={
            "locked": locked,
            "automatic_lock_exempt": automatic_lock_window if not locked else False,
        }
    )
    return _replace_assignment_clinic_slots(schedule, assignment_index, slots)


def resident_clinic_target_conflicts(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    source_slot: AssignedClinic | None = None,
    clinic_occupancy: ClinicOccupancy | None = None,
) -> tuple[str, ...]:
    """Explain hard and rule-based conflicts for a clinic drop target."""
    resident = instance.residents_by_id.get(resident_id)
    if resident is None:
        return (f"Unknown resident {resident_id!r}.",)
    if week < 1 or week > instance.calendar.weeks:
        return (f"Week {week} is outside the academic calendar.",)

    reasons: list[str] = []
    try:
        assignment = _resident_assignment_for_week(schedule, resident_id, week)
    except ValueError as exc:
        return (str(exc),)

    if week in resident.vacation_weeks:
        reasons.append("This resident is on vacation during this week.")
    if instance.resident_clinic_is_blocked(resident.id, week, weekday, session):
        reasons.append(f"{weekday.value.title()} {session.value} is blocked for this resident.")
    if instance.is_academic_half_day(week, weekday, session):
        reasons.append("Academic Half Day is fixed and cannot be rescheduled.")

    rotation = instance.rotation(assignment.rotation_id)
    if rotation.away:
        reasons.append(f"{rotation.code} is an Away rotation and cannot include clinic.")

    allowed_slots = _resident_allowed_clinic_slots(instance, resident, assignment)
    if allowed_slots and (weekday, session) not in allowed_slots:
        session_label = "AM" if session is Session.MORNING else "PM"
        reasons.append(
            f"{rotation.code} and this resident's clinic rules do not allow "
            f"{weekday.value.title()} {session_label}."
        )

    if source_slot is not None and not source_slot.admin:
        policy = instance.clinic_policy
        site_id = source_slot.site or policy.primary_site_id
        allowed_sites = policy.resolve_site_ids(source_slot.allowed_sites)
        if allowed_sites and site_id not in allowed_sites:
            reasons.append(
                f"{policy.site_name(site_id)} is outside this clinic block's allowed sites."
            )
        reasons.extend(
            _resident_clinic_preceptor_conflicts(
                instance,
                schedule,
                resident_id=resident_id,
                week=week,
                weekday=weekday,
                session=session,
                source_slot=source_slot,
                clinic_occupancy=clinic_occupancy,
            )
        )

    rule = _assignment_clinic_rule(instance, assignment)
    if source_slot is not None and not source_slot.admin and rule is not None:
        board = (
            clinic_occupancy
            if clinic_occupancy is not None
            else occupancy(
                instance,
                schedule,
            )
        )
        people = board.get((week, weekday, session), [])
        same_rotation_ids = {
            person.resident_id
            for person in people
            if person.resident_id != resident_id
            and schedule.week_grid.get(person.resident_id, {}).get(str(week))
            == assignment.rotation_id
        }
        if rule.max_concurrent is not None and len(same_rotation_ids) + 1 > rule.max_concurrent:
            noun = "resident" if rule.max_concurrent == 1 else "residents"
            reasons.append(
                f"{rotation.code} allows at most {rule.max_concurrent} {noun} in clinic "
                "at the same time."
            )
        pgy_limit = rule.max_concurrent_for_pgy(resident.pgy)
        if pgy_limit is not None:
            same_pgy_count = sum(
                1
                for other_id in same_rotation_ids
                if instance.residents_by_id.get(other_id) is not None
                and instance.residents_by_id[other_id].pgy == resident.pgy
            )
            if same_pgy_count + 1 > pgy_limit:
                noun = "resident" if pgy_limit == 1 else "residents"
                reasons.append(
                    f"{rotation.code} allows at most {pgy_limit} "
                    f"{instance.training_level_label(resident.pgy, compact=True)} "
                    f"{noun} in clinic at the same time."
                )

    return tuple(dict.fromkeys(reasons))


def _resident_clinic_preceptor_conflicts(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    source_slot: AssignedClinic,
    clinic_occupancy: ClinicOccupancy | None = None,
) -> tuple[str, ...]:
    """Return site closure and staffing conflicts that cannot be overridden."""
    if source_slot.admin:
        return ()
    policy = instance.clinic_policy
    site_id = source_slot.site or policy.primary_site_id
    if site_id not in policy.site_ids:
        return (f"Clinic site {site_id!r} is not configured.",)
    calendar_day = clinic_slot_date(instance.calendar.first_week_start, week, weekday)
    site_name = policy.site_name(site_id)
    if policy.is_site_closed(site_id, calendar_day):
        closure = policy.closure_on(calendar_day)
        suffix = f" ({closure.name})" if closure is not None and closure.name else ""
        return (f"{site_name} is closed on {calendar_day:%B} {calendar_day.day}{suffix}.",)
    maximum = policy.max_capacity_on(site_id, calendar_day, session)
    if maximum <= 0:
        return (
            f"{site_name} has no attending coverage for {weekday.value.title()} "
            f"{'AM' if session is Session.MORNING else 'PM'}.",
        )
    board = clinic_occupancy if clinic_occupancy is not None else occupancy(instance, schedule)
    people = board.get((week, weekday, session), [])
    filled = site_headcount(people, site_id)
    if any(
        person.resident_id == resident_id and occupant_site(person) == site_id for person in people
    ):
        # An occupied resident-calendar target is replaced or swapped out, so it
        # does not consume capacity while evaluating the incoming block.
        filled -= 1
    if filled >= maximum:
        return (f"{site_name} is at capacity for this half-day ({filled} of {maximum} residents).",)
    return ()


def _resident_clinic_hard_target_conflicts(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    source_slot: AssignedClinic,
    clinic_occupancy: ClinicOccupancy | None = None,
) -> tuple[str, ...]:
    """Return immutable or staffing conflicts that a manual edit cannot waive."""
    reasons: list[str] = []
    calendar_day = clinic_slot_date(
        instance.calendar.first_week_start,
        week,
        weekday,
    )
    special_rotations = instance.special_rotations_for_resident(
        resident_id,
        calendar_day=calendar_day,
        session=session,
    )
    if special_rotations:
        reasons.append(
            "This half-day is blocked by "
            + ", ".join(special.name for special in special_rotations)
            + "."
        )
    if instance.is_academic_half_day(week, weekday, session):
        reasons.append("Academic Half Day is fixed and cannot be rescheduled.")
    reasons.extend(
        _resident_clinic_preceptor_conflicts(
            instance,
            schedule,
            resident_id=resident_id,
            week=week,
            weekday=weekday,
            session=session,
            source_slot=source_slot,
            clinic_occupancy=clinic_occupancy,
        )
    )
    return tuple(dict.fromkeys(reasons))


def _clinic_slot_has_manual_override(
    instance: SchedulerInput,
    slot: AssignedClinic,
    *,
    placement_conflicts: tuple[str, ...],
) -> bool:
    current_site = slot.site or instance.clinic_policy.primary_site_id
    site_changed = (
        slot.manual_override_original_site is not None
        and current_site != slot.manual_override_original_site
    )
    return slot.manual_override_added or site_changed or bool(placement_conflicts)


def move_resident_clinic_slot(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    source_week: int,
    source_weekday: Weekday,
    source_session: Session,
    target_week: int,
    target_weekday: Weekday,
    target_session: Session,
    today: date | None = None,
) -> Schedule:
    """Move one unlocked clinic occurrence, recording rule exceptions as overrides."""
    if source_week != target_week:
        raise ValueError("Clinic blocks can only be moved within the same week.")
    if (source_weekday, source_session) == (target_weekday, target_session):
        return schedule

    assignment_index, assignment = _resident_assignment_index(
        schedule,
        resident_id,
        source_week,
    )
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    source_index = _resident_clinic_slot_index(
        slots,
        source_week,
        source_weekday,
        source_session,
    )
    source_slot = slots[source_index]
    if clinic_slot_is_locked(instance, source_slot, source_week, today=today):
        raise ValueError("This clinic block is locked. Unlock it before moving it.")
    target_indices = [
        index
        for index, slot in enumerate(slots)
        if slot.week == target_week
        and slot.weekday is target_weekday
        and slot.session is target_session
    ]
    if len(target_indices) > 1:
        raise ValueError("More than one clinic block occupies the target half-day.")
    target_index = target_indices[0] if target_indices else None
    target_slot = slots[target_index] if target_index is not None else None
    if target_slot is not None and _clinic_slot_content_key(source_slot) == (
        _clinic_slot_content_key(target_slot)
    ):
        return schedule
    if target_slot is not None and clinic_slot_is_locked(
        instance,
        target_slot,
        target_week,
        today=today,
    ):
        raise ValueError("The destination block is locked. Unlock it before swapping.")

    clinic_occupancy = occupancy(instance, schedule)
    target_conflicts = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=resident_id,
        week=target_week,
        weekday=target_weekday,
        session=target_session,
        source_slot=source_slot,
        clinic_occupancy=clinic_occupancy,
    )
    target_hard_conflicts = _resident_clinic_hard_target_conflicts(
        instance,
        schedule,
        resident_id=resident_id,
        week=target_week,
        weekday=target_weekday,
        session=target_session,
        source_slot=source_slot,
        clinic_occupancy=clinic_occupancy,
    )
    source_conflicts: tuple[str, ...] = ()
    source_hard_conflicts: tuple[str, ...] = ()
    if target_slot is not None:
        source_conflicts = resident_clinic_target_conflicts(
            instance,
            schedule,
            resident_id=resident_id,
            week=source_week,
            weekday=source_weekday,
            session=source_session,
            source_slot=target_slot,
            clinic_occupancy=clinic_occupancy,
        )
        source_hard_conflicts = _resident_clinic_hard_target_conflicts(
            instance,
            schedule,
            resident_id=resident_id,
            week=source_week,
            weekday=source_weekday,
            session=source_session,
            source_slot=target_slot,
            clinic_occupancy=clinic_occupancy,
        )
    hard_conflicts = target_hard_conflicts + source_hard_conflicts
    if hard_conflicts:
        raise ValueError(" ".join(dict.fromkeys(hard_conflicts)))

    target_override_reasons = tuple(
        reason for reason in target_conflicts if reason not in target_hard_conflicts
    )
    source_override_reasons = tuple(
        reason for reason in source_conflicts if reason not in source_hard_conflicts
    )

    slots[source_index] = source_slot.model_copy(
        update={
            "week": target_week,
            "weekday": target_weekday,
            "session": target_session,
            "manual_override": _clinic_slot_has_manual_override(
                instance,
                source_slot,
                placement_conflicts=target_override_reasons,
            ),
        }
    )
    if target_index is not None and target_slot is not None:
        slots[target_index] = target_slot.model_copy(
            update={
                "week": source_week,
                "weekday": source_weekday,
                "session": source_session,
                "manual_override": _clinic_slot_has_manual_override(
                    instance,
                    target_slot,
                    placement_conflicts=source_override_reasons,
                ),
            }
        )
    slots.sort(key=_clinic_slot_sort_key)
    updated = _replace_assignment_clinic_slots(schedule, assignment_index, slots)
    return _validate_clinic_schedule_change(instance, updated)


def add_resident_clinic_slot(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    site_id: str,
) -> Schedule:
    """Add one explicit clinic occurrence as a manual override."""
    if site_id not in instance.clinic_policy.site_ids:
        raise ValueError(f"Clinic site {site_id!r} is not configured.")
    assignment_index, assignment = _resident_assignment_index(schedule, resident_id, week)
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    if any(
        slot.week == week and slot.weekday is weekday and slot.session is session for slot in slots
    ):
        raise ValueError("A clinic block already occupies this half-day.")
    candidate = AssignedClinic(
        weekday=weekday,
        session=session,
        site=site_id,
        allowed_sites=[site_id],
        manual_override=True,
        manual_override_added=True,
        week=week,
    )
    clinic_occupancy = occupancy(instance, schedule)
    hard_conflicts = _resident_clinic_hard_target_conflicts(
        instance,
        schedule,
        resident_id=resident_id,
        week=week,
        weekday=weekday,
        session=session,
        source_slot=candidate,
        clinic_occupancy=clinic_occupancy,
    )
    if hard_conflicts:
        raise ValueError(" ".join(hard_conflicts))

    baseline = _manual_clinic_baseline_for_change(assignment, slots, week)
    slots.append(candidate)
    slots.sort(key=_clinic_slot_sort_key)
    baselines = _updated_manual_clinic_baselines(
        assignment,
        week=week,
        baseline=baseline,
        current=sum(slot.week == week for slot in slots),
    )
    updated = _replace_assignment_clinic_slots(
        schedule,
        assignment_index,
        slots,
        manual_clinic_baselines=baselines,
    )
    return _validate_clinic_schedule_change(instance, updated)


def remove_resident_clinic_slot(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    today: date | None = None,
) -> Schedule:
    """Remove one unlocked clinic occurrence and retain its weekly baseline."""
    assignment_index, assignment = _resident_assignment_index(schedule, resident_id, week)
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    slot_index = _resident_clinic_slot_index(slots, week, weekday, session)
    if clinic_slot_is_locked(instance, slots[slot_index], week, today=today):
        raise ValueError("This clinic block is locked. Unlock it before deleting it.")
    baseline = _manual_clinic_baseline_for_change(assignment, slots, week)
    slots.pop(slot_index)
    baselines = _updated_manual_clinic_baselines(
        assignment,
        week=week,
        baseline=baseline,
        current=sum(slot.week == week for slot in slots),
    )
    updated = _replace_assignment_clinic_slots(
        schedule,
        assignment_index,
        slots,
        manual_clinic_baselines=baselines,
    )
    return _validate_clinic_schedule_change(instance, updated)


def change_resident_clinic_slot_site(
    instance: SchedulerInput,
    schedule: Schedule,
    *,
    resident_id: str,
    week: int,
    weekday: Weekday,
    session: Session,
    site_id: str,
    today: date | None = None,
) -> Schedule:
    """Reassign an unlocked clinic occurrence to a staffed site as an override."""
    if site_id not in instance.clinic_policy.site_ids:
        raise ValueError(f"Clinic site {site_id!r} is not configured.")
    assignment_index, assignment = _resident_assignment_index(schedule, resident_id, week)
    slots = _materialized_assignment_clinic_slots(instance, assignment)
    slot_index = _resident_clinic_slot_index(slots, week, weekday, session)
    slot = slots[slot_index]
    if slot.admin:
        raise ValueError("Administrative time does not have a clinic site.")
    if clinic_slot_is_locked(instance, slot, week, today=today):
        raise ValueError("This clinic block is locked. Unlock it before changing its site.")
    policy = instance.clinic_policy
    current_site = slot.site or policy.primary_site_id
    original_site = slot.manual_override_original_site or current_site
    if current_site == site_id and slot.manual_override_original_site is None:
        return schedule
    resetting_site = site_id == original_site
    candidate = slot.model_copy(
        update={
            "site": site_id,
            "manual_override_original_site": None if resetting_site else original_site,
        }
    )
    clinic_occupancy = occupancy(instance, schedule)
    hard_conflicts = _resident_clinic_hard_target_conflicts(
        instance,
        schedule,
        resident_id=resident_id,
        week=week,
        weekday=weekday,
        session=session,
        source_slot=candidate,
        clinic_occupancy=clinic_occupancy,
    )
    if hard_conflicts:
        raise ValueError(" ".join(hard_conflicts))
    all_conflicts = resident_clinic_target_conflicts(
        instance,
        schedule,
        resident_id=resident_id,
        week=week,
        weekday=weekday,
        session=session,
        source_slot=candidate,
        clinic_occupancy=clinic_occupancy,
    )
    override_reasons = tuple(reason for reason in all_conflicts if reason not in hard_conflicts)
    candidate = candidate.model_copy(
        update={
            "manual_override": _clinic_slot_has_manual_override(
                instance,
                candidate,
                placement_conflicts=override_reasons,
            )
        }
    )
    slots[slot_index] = candidate
    slots.sort(key=_clinic_slot_sort_key)
    updated = _replace_assignment_clinic_slots(schedule, assignment_index, slots)
    return _validate_clinic_schedule_change(instance, updated)


def _clinic_slot_content_key(slot: AssignedClinic) -> tuple:
    """Identity of a clinic block apart from the half-day it occupies."""
    return (
        slot.site,
        tuple(slot.allowed_sites),
        slot.admin,
    )


def _resident_assignment_for_week(
    schedule: Schedule,
    resident_id: str,
    week: int,
) -> Assignment:
    return _resident_assignment_index(schedule, resident_id, week)[1]


def _resident_assignment_index(
    schedule: Schedule,
    resident_id: str,
    week: int,
) -> tuple[int, Assignment]:
    matches = [
        (index, assignment)
        for index, assignment in enumerate(schedule.assignments)
        if assignment.resident_id == resident_id and week in assignment.weeks
    ]
    if not matches:
        raise ValueError(f"This resident has no rotation assignment in week {week}.")
    if len(matches) > 1:
        raise ValueError(f"This resident has overlapping assignments in week {week}.")
    return matches[0]


def _resident_clinic_slot_index(
    slots: list[AssignedClinic],
    week: int,
    weekday: Weekday,
    session: Session,
) -> int:
    matches = [
        index
        for index, slot in enumerate(slots)
        if slot.week == week and slot.weekday is weekday and slot.session is session
    ]
    if not matches:
        raise ValueError("The clinic block no longer exists in that half-day.")
    if len(matches) > 1:
        raise ValueError("More than one clinic block occupies that half-day.")
    return matches[0]


def _materialized_assignment_clinic_slots(
    instance: SchedulerInput,
    assignment: Assignment,
) -> list[AssignedClinic]:
    slots = assignment.clinic_slots
    if any(slot.week is not None for slot in slots):
        return sorted(
            [slot for slot in slots if slot.week is not None],
            key=_clinic_slot_sort_key,
        )

    if assignment.kind is not RotationKind.CLINIC:
        return sorted(
            [slot.model_copy(update={"week": week}) for week in assignment.weeks for slot in slots],
            key=_clinic_slot_sort_key,
        )

    rotation = instance.rotation(assignment.rotation_id)
    templates = {(slot.weekday, slot.session): slot for slot in slots}
    configured_sites = {
        (slot.weekday, slot.session): slot.sites
        for slot in (rotation.clinic.expanded_slots() if rotation.clinic is not None else [])
        if slot.weekday is not None and slot.session is not None
    }
    materialized: list[AssignedClinic] = []
    for week in assignment.weeks:
        for weekday, session in clinic_kind_slots_for_week(
            instance,
            week,
            assignment.rotation_id,
        ):
            template = templates.get((weekday, session))
            admin = bool(template and template.admin)
            allowed_sites = (
                list(template.allowed_sites)
                if template is not None and template.allowed_sites
                else list(configured_sites.get((weekday, session), []))
            )
            resolved_sites = instance.clinic_policy.resolve_site_ids(allowed_sites)
            site = (
                None
                if admin
                else (
                    template.site
                    if template is not None and template.site is not None
                    else (
                        resolved_sites[0]
                        if resolved_sites
                        else instance.clinic_policy.primary_site_id
                    )
                )
            )
            materialized.append(
                AssignedClinic(
                    weekday=weekday,
                    session=session,
                    site=site,
                    allowed_sites=allowed_sites,
                    admin=admin,
                    locked=bool(template and template.locked),
                    automatic_lock_exempt=bool(template and template.automatic_lock_exempt),
                    manual_override=bool(template and template.manual_override),
                    manual_override_added=bool(template and template.manual_override_added),
                    manual_override_original_site=(
                        template.manual_override_original_site if template is not None else None
                    ),
                    week=week,
                )
            )
    return sorted(materialized, key=_clinic_slot_sort_key)


def _replace_assignment_clinic_slots(
    schedule: Schedule,
    assignment_index: int,
    slots: list[AssignedClinic],
    *,
    manual_clinic_baselines: dict[int, int] | None = None,
) -> Schedule:
    assignments = list(schedule.assignments)
    update: dict = {"clinic_slots": slots}
    if manual_clinic_baselines is not None:
        update["manual_clinic_baselines"] = manual_clinic_baselines
    assignments[assignment_index] = assignments[assignment_index].model_copy(update=update)
    return schedule.model_copy(update={"assignments": assignments})


def _manual_clinic_baseline_for_change(
    assignment: Assignment,
    slots: list[AssignedClinic],
    week: int,
) -> int:
    return assignment.manual_clinic_baselines.get(
        week,
        sum(slot.week == week for slot in slots),
    )


def _updated_manual_clinic_baselines(
    assignment: Assignment,
    *,
    week: int,
    baseline: int,
    current: int,
) -> dict[int, int]:
    baselines = dict(assignment.manual_clinic_baselines)
    if current == baseline:
        baselines.pop(week, None)
    else:
        baselines[week] = baseline
    return baselines


def _validate_clinic_schedule_change(
    instance: SchedulerInput,
    schedule: Schedule,
) -> Schedule:
    try:
        validate_schedule_or_raise(instance, schedule)
    except ValueError as exc:
        message = str(exc).removeprefix("schedule does not match instance: ")
        raise ValueError(message) from None
    return schedule


def _assignment_clinic_rule(instance: SchedulerInput, assignment: Assignment):
    rotation = instance.rotation(assignment.rotation_id)
    return rotation.clinic


def _resident_allowed_clinic_slots(
    instance: SchedulerInput,
    resident: Resident,
    assignment: Assignment,
) -> set[tuple[Weekday, Session]]:
    rule = _assignment_clinic_rule(instance, assignment)
    allowed = {
        (slot.weekday, slot.session)
        for slot in (rule.expanded_slots() if rule is not None else [])
        if slot.weekday is not None and slot.session is not None
    }
    allowed.update((slot.weekday, slot.session) for slot in resident.clinic_half_days)
    return allowed


def _clinic_slot_sort_key(slot: AssignedClinic) -> tuple[int, int, int, bool]:
    return (
        slot.week or 0,
        list(Weekday).index(slot.weekday),
        list(Session).index(slot.session),
        slot.admin,
    )


def _resident_clinic_session_key(weekday: Weekday, session: Session) -> str:
    return f"{weekday.value}_{session.value}"


def _compact_week_ranges(weeks: list[int]) -> str:
    ordered = sorted(set(weeks))
    if not ordered:
        return ""
    ranges: list[str] = []
    start = previous = ordered[0]
    for week in ordered[1:]:
        if week == previous + 1:
            previous = week
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = week
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return ", ".join(ranges)


def _vacation_date_range(monday: date, sunday: date) -> str:
    if monday.year == sunday.year and monday.month == sunday.month:
        return f"{monday:%b} {monday.day}–{sunday.day}, {monday:%Y}"
    if monday.year == sunday.year:
        return f"{monday:%b} {monday.day}–{sunday:%b} {sunday.day}, {monday:%Y}"
    return f"{monday:%b} {monday.day}, {monday:%Y}–{sunday:%b} {sunday.day}, {sunday:%Y}"
