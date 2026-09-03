"""Clinic schedule occupancy for every configured weekly session."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date, timedelta

from rbs.models.clinic import ClinicPolicy, clinic_slot_date
from rbs.models.curriculum import default_training_level_code
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.models.schedule import Assignment, Schedule
from rbs.models.special import SpecialRotation, SpecialRotationKind
from rbs.ui.grid import visible_week_numbers, week_monday

ACADEMIC_LABEL = "Academic Half Day"

WEEKDAY_SHORT = {
    Weekday.MONDAY: "Mon",
    Weekday.TUESDAY: "Tue",
    Weekday.WEDNESDAY: "Wed",
    Weekday.THURSDAY: "Thu",
    Weekday.FRIDAY: "Fri",
    Weekday.SATURDAY: "Sat",
    Weekday.SUNDAY: "Sun",
}

SESSION_SHORT = {
    Session.MORNING: "AM",
    Session.AFTERNOON: "PM",
}


@dataclass(frozen=True)
class ClinicOccupant:
    resident_id: str
    name: str
    pgy: int
    admin: bool = False
    site: str | None = None
    site_name: str | None = None
    site_color: str | None = None
    site_light_color: str | None = None
    manual_override: bool = False
    training_level_code: str | None = None
    training_level_order: int | None = None

    def display_label(self) -> str:
        code = self.training_level_code or default_training_level_code(self.pgy)
        return f"{code} {self.name}"

    def label(self) -> str:
        text = self.display_label()
        if self.admin:
            return text + " · Admin"
        if self.site is not None:
            text += f" · {self.site_name or self.site}"
        return text


@dataclass(frozen=True)
class ClinicClosureView:
    """Closure details scoped to the clinic sites visible in one calendar view."""

    closed_site_ids: tuple[str, ...] = ()
    closed_site_names: tuple[str, ...] = ()
    name: str = ""
    all_selected_sites_closed: bool = False

    @property
    def is_closed(self) -> bool:
        return bool(self.closed_site_ids)

    @property
    def is_partial(self) -> bool:
        return self.is_closed and not self.all_selected_sites_closed

    def label(self) -> str:
        if self.all_selected_sites_closed:
            status = "Closed"
        else:
            status = f"{', '.join(self.closed_site_names)} closed"
        return f"{self.name} · {status}" if self.name else status


def clinic_weekdays(instance: SchedulerInput | None = None) -> tuple[Weekday, ...]:
    """Return weekdays shown by the Clinic calendar.

    Weekdays remain visible for a familiar work-week view; weekend columns are
    added when any capacity, override, or rotation rule uses them.
    """
    days = set(WEEKDAYS_MF)
    if instance is not None:
        for site in instance.clinic_policy.sites:
            days.update(half_day.weekday for half_day in site.half_days)
            days.update(
                Weekday(list(Weekday)[override.date.weekday()].value)
                for override in site.capacity_overrides
            )
        for rotation in instance.rotations:
            if rotation.clinic is not None:
                days.update(
                    slot.weekday for slot in rotation.clinic.slots if slot.weekday is not None
                )
        for resident in instance.residents:
            days.update(half_day.weekday for half_day in resident.clinic_half_days)
        days.update(
            tuple(Weekday)[special.start_date.weekday()]
            for special in instance.special_rotations
            if special.kind is SpecialRotationKind.EVENT
        )
    return tuple(day for day in Weekday if day in days)


def half_days(
    instance: SchedulerInput | None = None,
) -> list[tuple[Weekday, Session]]:
    return [(day, session) for day in clinic_weekdays(instance) for session in Session]


def is_academic(policy: ClinicPolicy, weekday: Weekday, session: Session) -> bool:
    """Whether a slot matches the recurring program academic half-day."""
    return weekday is policy.academic.weekday and session is policy.academic.session


def is_academic_week(
    instance: SchedulerInput,
    week: int,
    weekday: Weekday,
    session: Session,
) -> bool:
    """Whether a slot is Academic after applying a week-specific override."""
    return instance.is_academic_half_day(week, weekday, session)


def clinic_kind_slots(instance: SchedulerInput) -> list[tuple[Weekday, Session]]:
    """Configured recurring sessions for all dedicated Clinic blocks."""
    slots = {
        (slot.weekday, slot.session)
        for rotation in instance.rotations
        if rotation.kind is RotationKind.CLINIC
        and rotation.clinic is not None
        and not rotation.clinic_hours_disabled
        for slot in rotation.clinic.slots
        if slot.weekday is not None
        and slot.session is not None
        and not is_academic(instance.clinic_policy, slot.weekday, slot.session)
    }
    return [slot for slot in half_days(instance) if slot in slots]


def clinic_kind_slots_for_week(
    instance: SchedulerInput,
    week: int,
    rotation_id: str | None = None,
) -> list[tuple[Weekday, Session]]:
    """Configured Clinic-block sessions after that week's Academic override."""
    rotations = [
        rotation
        for rotation in instance.rotations
        if rotation.kind is RotationKind.CLINIC
        and (rotation_id is None or rotation.id == rotation_id)
    ]
    configured = {
        (slot.weekday, slot.session)
        for rotation in rotations
        if rotation.clinic is not None and not rotation.clinic_hours_disabled
        for slot in rotation.clinic.slots
        if slot.weekday is not None and slot.session is not None
    }
    return [
        slot
        for slot in half_days(instance)
        if slot in configured and not is_academic_week(instance, week, *slot)
    ]


def clinic_closure_view(
    policy: ClinicPolicy,
    calendar_day: date,
    site: str | None = None,
) -> ClinicClosureView:
    """Return full/partial closure state for the sites selected in a calendar view."""
    closure = policy.closure_on(calendar_day)
    if closure is None:
        return ClinicClosureView()
    selected_sites = (site,) if site is not None else policy.site_ids
    configured_closed = set(closure.sites)
    closed_ids = tuple(site_id for site_id in selected_sites if site_id in configured_closed)
    if not closed_ids:
        return ClinicClosureView()
    return ClinicClosureView(
        closed_site_ids=closed_ids,
        closed_site_names=tuple(policy.site_name(site_id) for site_id in closed_ids),
        name=closure.name,
        all_selected_sites_closed=len(closed_ids) == len(selected_sites),
    )


def occupancy(
    instance: SchedulerInput, schedule: Schedule | None
) -> dict[tuple[int, Weekday, Session], list[ClinicOccupant]]:
    """Resident names in clinic for each (week, weekday, session).

    Academic time is empty. Clinic blocks fill their configured sessions, with
    configured Admin sessions marked separately. Overlay sessions fill only
    their selected times. Vacation weeks and individual days off are omitted
    unless an occurrence was deliberately retained as a manual override.
    """
    policy = instance.clinic_policy
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]] = {
        (week, day, session): []
        for week in range(1, instance.calendar.weeks + 1)
        for day, session in half_days(instance)
    }
    if schedule is None or schedule.is_empty():
        return board

    residents = instance.residents_by_id
    seen: dict[tuple[int, Weekday, Session], set[str]] = {key: set() for key in board}

    for assignment in schedule.assignments:
        resident = residents.get(assignment.resident_id)
        if resident is None:
            continue
        try:
            rotation = instance.rotation(assignment.rotation_id)
        except KeyError:
            continue
        vacation = set(resident.vacation_weeks)
        for occupant, week, weekday, session in _occupants_for(
            assignment,
            resident,
            instance,
        ):
            if rotation.away and not occupant.manual_override:
                continue
            if week in vacation and not occupant.manual_override:
                continue
            calendar_day = clinic_slot_date(
                instance.calendar.first_week_start,
                week,
                weekday,
            )
            if instance.special_rotations_for_resident(
                resident.id,
                calendar_day=calendar_day,
                session=session,
            ):
                continue
            if not occupant.manual_override and calendar_day in resident.days_off:
                continue
            if is_academic_week(instance, week, weekday, session):
                continue
            if occupant.site is not None and policy.is_site_closed(
                occupant.site,
                calendar_day,
            ):
                continue
            key = (week, weekday, session)
            if key not in board:
                continue
            if occupant.resident_id in seen[key]:
                continue
            seen[key].add(occupant.resident_id)
            board[key].append(occupant)

    for people in board.values():
        people.sort(
            key=lambda person: (
                person.training_level_order
                if person.training_level_order is not None
                else person.pgy,
                person.admin,
                person.site is None,
                person.site or "",
                person.name,
                person.resident_id,
            )
        )
    return board


def clinic_headcount(people: list[ClinicOccupant]) -> int:
    """Residents who need an attending (Admin does not)."""
    return sum(1 for person in people if not person.admin)


def occupant_site(person: ClinicOccupant) -> str | None:
    if person.admin:
        return None
    return person.site


def site_headcount(people: list[ClinicOccupant], site: str) -> int:
    return sum(1 for person in people if occupant_site(person) == site)


def occupants_for_site(people: list[ClinicOccupant], site: str | None) -> list[ClinicOccupant]:
    """Return occupants visible in an all-sites or single-site view.

    Admin time has no clinic site, so it is shown only in the all-sites view.
    """
    if site is None:
        return people
    return [person for person in people if occupant_site(person) == site]


def calendar_occupants(people: list[ClinicOccupant], policy: ClinicPolicy) -> list[ClinicOccupant]:
    """Order a calendar session by site, training level, then last-name initial."""
    site_order = {site_id: index for index, site_id in enumerate(policy.site_ids)}

    def sort_key(person: ClinicOccupant) -> tuple:
        last_name = person.name.rsplit(" ", 1)[-1]
        site_index = len(site_order) if person.admin else site_order.get(person.site or "", 999)
        return (
            site_index,
            person.training_level_order if person.training_level_order is not None else person.pgy,
            last_name[:1].casefold(),
            last_name.casefold(),
            person.name.casefold(),
            person.resident_id,
        )

    return sorted(people, key=sort_key)


def attending_load(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    site: str | None = None,
) -> tuple[int, int]:
    """Peak attendings in any half-day, and total attending-sessions (per site)."""
    board = occupancy(instance, schedule)
    policy = instance.clinic_policy
    sites = (site,) if site is not None else policy.site_ids
    peak = 0
    total = 0
    for people in board.values():
        half_day = 0
        for this_site in sites:
            needed = policy.attendings_needed(
                site_headcount(people, this_site),
                this_site,
            )
            half_day += needed
            total += needed
        peak = max(peak, half_day)
    return peak, total


def weekly_attending_sessions(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    site: str | None = None,
) -> dict[int, int]:
    """Attending-sessions per week at one site or the configured primary site."""
    board = occupancy(instance, schedule)
    policy = instance.clinic_policy
    selected_site = site or policy.primary_site_id
    by_week: dict[int, int] = {week: 0 for week in range(1, instance.calendar.weeks + 1)}
    for (week, _weekday, _session), people in board.items():
        by_week[week] += policy.attendings_needed(
            site_headcount(people, selected_site),
            selected_site,
        )
    return by_week


def render_clinic_html(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
    show_legend: bool = True,
) -> str:
    board = occupancy(instance, schedule)
    start = instance.calendar.first_week_start
    policy = instance.clinic_policy
    weekdays = clinic_weekdays(instance)
    weeks = visible_week_numbers(
        start,
        instance.calendar.weeks,
        show_past_weeks=show_past_weeks,
        today=today,
    )
    calendar = "".join(_calendar_week_html(instance, board, week, site) for week in weeks)
    legend = render_clinic_legend_html(policy) if show_legend else ""
    return (
        f"{legend}"
        '<div class="rbs-clinic-wrap rbs-clinic-calendar-wrap">'
        f'<div class="rbs-clinic-calendar" role="grid" '
        f'style="--rbs-clinic-days:{len(weekdays)}">{calendar}</div></div>'
    )


def _calendar_week_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    site: str | None,
) -> str:
    monday = week_monday(instance.calendar.first_week_start, week)
    weekdays = clinic_weekdays(instance)
    days = "".join(
        _calendar_day_html(
            instance,
            board,
            week,
            weekday,
            monday + timedelta(days=list(Weekday).index(weekday)),
            site,
        )
        for weekday in weekdays
    )
    final_day = monday + timedelta(days=list(Weekday).index(weekdays[-1]))
    range_label = f"{monday:%b} {monday.day} - {final_day:%b} {final_day.day}"
    return (
        f'<div class="rbs-clinic-week" role="row" aria-label="{html.escape(range_label)}">'
        f"{days}</div>"
    )


def _calendar_day_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    weekday: Weekday,
    calendar_day: date,
    site: str | None,
) -> str:
    closure = clinic_closure_view(instance.clinic_policy, calendar_day, site)
    if closure.all_selected_sites_closed:
        sessions = "".join(
            _closed_session_html(instance, calendar_day, session, closure) for session in Session
        )
    else:
        sessions = "".join(
            _calendar_session_html(
                instance,
                board,
                week,
                weekday,
                session,
                calendar_day,
                site,
            )
            for session in Session
        )
    full_label = (
        f"{weekday.value.title()}, {calendar_day:%B} {calendar_day.day}, {calendar_day.year}"
    )
    classes = ["rbs-clinic-day"]
    closure_badge = ""
    if closure.all_selected_sites_closed:
        classes.append("closure-full")
    elif closure.is_partial:
        classes.append("closure-partial")
    if closure.is_closed:
        closure_label = closure.label()
        full_label += f"; {closure_label}"
        badge_kind = "full" if closure.all_selected_sites_closed else "partial"
        closure_badge = (
            f'<span class="rbs-clinic-closure-badge {badge_kind}">'
            f"{html.escape(closure_label)}</span>"
        )
    class_names = " ".join(classes)
    return (
        f'<section class="{class_names}" role="gridcell" '
        f'aria-label="{html.escape(full_label)}">'
        '<header class="rbs-clinic-day-header">'
        '<span class="rbs-clinic-day-heading">'
        f'<span class="rbs-clinic-day-name">{html.escape(weekday.value.title())}</span>'
        f"{closure_badge}</span>"
        f'<time datetime="{calendar_day.isoformat()}">{calendar_day:%b} {calendar_day.day}</time>'
        "</header>"
        f'<div class="rbs-clinic-day-sessions">{sessions}</div></section>'
    )


def _closed_session_html(
    instance: SchedulerInput,
    calendar_day: date,
    session: Session,
    closure: ClinicClosureView,
) -> str:
    session_label = SESSION_SHORT[session]
    title = html.escape(closure.label())
    events = "".join(
        _special_event_html(instance, special)
        for special in special_events_for_slot(instance, calendar_day, session)
    )
    return (
        f'<section class="rbs-clinic-session closure" title="{title}">'
        '<div class="rbs-clinic-session-heading">'
        f'<span class="rbs-clinic-session-label">{session_label}</span></div>'
        f'<div class="rbs-clinic-session-body">Closed{events}</div></section>'
    )


def special_events_for_slot(
    instance: SchedulerInput,
    calendar_day: date,
    session: Session,
) -> tuple[SpecialRotation, ...]:
    """Return Clinic Calendar events occupying one dated half-day."""
    return tuple(
        special
        for special in instance.special_rotations
        if special.kind is SpecialRotationKind.EVENT and special.blocks(calendar_day, session)
    )


def _calendar_session_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    weekday: Weekday,
    session: Session,
    calendar_day: date,
    site: str | None,
) -> str:
    policy = instance.clinic_policy
    session_label = SESSION_SHORT[session]
    title_prefix = f"{calendar_day:%b} {calendar_day.day} · {session_label}"
    special_events = special_events_for_slot(instance, calendar_day, session)
    special_event_html = "".join(
        _special_event_html(instance, special) for special in special_events
    )
    if is_academic_week(instance, week, weekday, session):
        title = ACADEMIC_LABEL
        if special_events:
            title += " · " + " · ".join(
                f"{special.name}: "
                + ", ".join(
                    instance.residents_by_id[resident_id].name
                    for resident_id in special.resident_ids
                )
                for special in special_events
            )
        return (
            f'<section class="rbs-clinic-session academic" title="{html.escape(title)}">'
            '<div class="rbs-clinic-session-heading">'
            f'<span class="rbs-clinic-session-label">{session_label}</span></div>'
            f'<div class="rbs-clinic-session-body">{html.escape(ACADEMIC_LABEL)}'
            f"{special_event_html}</div>"
            "</section>"
        )
    people = calendar_occupants(
        occupants_for_site(board[(week, weekday, session)], site),
        policy,
    )
    names = "".join(_person_html(person) for person in people)
    attending_details = []
    attending_markers = []
    sites = (site,) if site is not None else policy.site_ids
    for clinic_site in sites:
        needed = policy.attendings_needed(
            site_headcount(people, clinic_site),
            clinic_site,
        )
        if needed:
            site_config = policy.site(clinic_site)
            attending_details.append(
                f"{needed} attending{'s' if needed != 1 else ''} at {site_config.name}"
            )
            attending_markers.append(
                '<span class="rbs-clinic-session-att" '
                f'style="--rbs-clinic-att-color:{site_config.color}">'
                f"{needed} {html.escape(site_config.name)}</span>"
            )
    title_bits = [title_prefix]
    if people:
        title_bits.append(", ".join(person.label() for person in people))
    title_bits.extend(attending_details)
    title_bits.extend(
        f"{special.name}: "
        + ", ".join(
            instance.residents_by_id[resident_id].name for resident_id in special.resident_ids
        )
        for special in special_events
    )
    title = html.escape(" · ".join(title_bits))
    attending_html = ""
    if attending_markers:
        attending_html = (
            '<span class="rbs-clinic-session-attending" '
            'aria-label="Attending coverage">'
            f"{''.join(attending_markers)}</span>"
        )
    return (
        f'<section class="rbs-clinic-session" title="{title}">'
        '<div class="rbs-clinic-session-heading">'
        f'<span class="rbs-clinic-session-label">{session_label}</span>'
        f"{attending_html}</div>"
        f'<div class="rbs-clinic-session-body">{special_event_html}{names}</div></section>'
    )


def _special_event_html(
    instance: SchedulerInput,
    special: SpecialRotation,
) -> str:
    people = [instance.residents_by_id[resident_id] for resident_id in special.resident_ids]
    resident_labels = ", ".join(
        f"{instance.training_level_label(resident.pgy, compact=True)} {resident.name}"
        for resident in people
    )
    return (
        '<div class="rbs-clinic-special-event" '
        f'title="{html.escape(special.name + ": " + resident_labels)}">'
        f"<strong>{html.escape(special.name)}</strong>"
        f"<span>{html.escape(resident_labels)}</span></div>"
    )


def render_clinic_legend_html(policy: ClinicPolicy) -> str:
    swatches = "".join(
        '<span class="rbs-clinic-swatch site" '
        f'style="{_site_style(site.color, site.light_color)}">'
        f"{html.escape(site.name)}</span>"
        for site in policy.sites
    )
    swatches += '<span class="rbs-clinic-swatch admin">Admin</span>'
    swatches += '<span class="rbs-clinic-swatch special-event">Special event</span>'
    return (
        '<div class="rbs-clinic-legend">'
        f'<span class="rbs-clinic-key-label">Key</span>{swatches}</div>'
    )


def _person_html(person: ClinicOccupant) -> str:
    classes = "rbs-clinic-person"
    if person.admin:
        classes += " admin"
    elif person.site is not None:
        classes += " site"
    style = ""
    if person.site_color and person.site_light_color:
        style = f' style="{_site_style(person.site_color, person.site_light_color)}"'
    return (
        f'<div class="{classes}"{style} title="{html.escape(person.label())}">'
        f"{_display_label_html(person)}</div>"
    )


def _site_style(color: str, light_color: str) -> str:
    return f"--rbs-clinic-site-color:{color};--rbs-clinic-site-tint:{light_color}"


def _display_label_html(person: ClinicOccupant) -> str:
    name_parts = person.name.rsplit(" ", 1)
    if len(name_parts) == 1:
        name_html = f'<strong class="rbs-clinic-last-name">{html.escape(person.name)}</strong>'
    else:
        given_names, last_name = name_parts
        name_html = (
            f"{html.escape(given_names)} "
            f'<strong class="rbs-clinic-last-name">{html.escape(last_name)}</strong>'
        )
    training_level = html.escape(
        person.training_level_code or default_training_level_code(person.pgy)
    )
    return f'<span class="rbs-clinic-training-level">{training_level}</span> {name_html}'


def _occupants_for(
    assignment: Assignment,
    resident: Resident,
    instance: SchedulerInput,
) -> list[tuple[ClinicOccupant, int, Weekday, Session]]:
    policy = instance.clinic_policy
    if any(slot.week is not None for slot in assignment.clinic_slots):
        rows: list[tuple[ClinicOccupant, int, Weekday, Session]] = []
        for slot in assignment.clinic_slots:
            if slot.week is None:
                continue
            rows.append(
                (
                    _occupant(
                        resident,
                        instance,
                        admin=slot.admin,
                        site=None if slot.admin else (slot.site or policy.primary_site_id),
                        manual_override=slot.manual_override,
                    ),
                    slot.week,
                    slot.weekday,
                    slot.session,
                )
            )
        return rows
    if assignment.kind is RotationKind.CLINIC:
        templates = {(slot.weekday, slot.session): slot for slot in assignment.clinic_slots}
        admin_keys = {
            (slot.weekday, slot.session) for slot in assignment.clinic_slots if slot.admin
        }
        rows = []
        for week in assignment.weeks:
            for weekday, session in clinic_kind_slots_for_week(
                instance,
                week,
                assignment.rotation_id,
            ):
                admin = (weekday, session) in admin_keys
                template = templates.get((weekday, session))
                rows.append(
                    (
                        _occupant(
                            resident,
                            instance,
                            admin=admin,
                            site=None if admin else policy.primary_site_id,
                            manual_override=bool(template and template.manual_override),
                        ),
                        week,
                        weekday,
                        session,
                    )
                )
        return rows
    rows = []
    for slot in assignment.clinic_slots:
        if slot.admin:
            continue
        for week in assignment.weeks:
            rows.append(
                (
                    _occupant(
                        resident,
                        instance,
                        site=slot.site or policy.primary_site_id,
                        manual_override=slot.manual_override,
                    ),
                    week,
                    slot.weekday,
                    slot.session,
                )
            )
    return rows


def _occupant(
    resident: Resident,
    instance: SchedulerInput,
    *,
    admin: bool = False,
    site: str | None = None,
    manual_override: bool = False,
) -> ClinicOccupant:
    policy = instance.clinic_policy
    site_config = None if admin or site is None else policy.site(site)
    return ClinicOccupant(
        resident_id=resident.id,
        name=resident.name,
        pgy=resident.pgy,
        training_level_code=instance.training_level_label(
            resident.pgy,
            compact=True,
        ),
        training_level_order=instance.training_level_sort_key(resident.pgy),
        admin=admin,
        site=site_config.id if site_config else None,
        site_name=site_config.name if site_config else None,
        site_color=site_config.color if site_config else None,
        site_light_color=site_config.light_color if site_config else None,
        manual_override=manual_override,
    )
