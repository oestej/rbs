"""HTML rendering for the clinic schedule projection."""

from __future__ import annotations

import html
from datetime import date, timedelta

from rbs.models.attending import ATTENDING_WORK_TYPE_LABELS, AttendingWorkType
from rbs.models.clinic import ClinicPolicy, ClinicStaffingMode
from rbs.models.curriculum import default_training_level_code
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import Assignment, Schedule
from rbs.models.special import SpecialRotation
from rbs.solver.attending_availability import schedule_capacity_view
from rbs.ui.clinic.details import clinic_details
from rbs.ui.clinic.projection import (
    ACADEMIC_LABEL,
    SESSION_SHORT,
    AttendingOccupant,
    ClinicClosureView,
    ClinicOccupant,
    ClinicScheduleView,
    attending_occupancy,
    calendar_occupants,
    clinic_closure_view,
    clinic_weekdays,
    is_academic_week,
    occupancy,
    occupants_for_site,
    site_capacity_points,
    special_events_for_slot,
)
from rbs.ui.schedule_projection import visible_week_numbers, week_monday

__all__ = ["render_clinic_html", "render_clinic_legend_html"]

DetailTemplates = dict[tuple[tuple[str, str], ...], int]


def render_clinic_html(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
    show_legend: bool = True,
    view: ClinicScheduleView = "residents",
) -> str:
    board = occupancy(instance, schedule) if view == "residents" else {}
    attending_board = (
        attending_occupancy(instance, schedule, site=site) if view == "attendings" else None
    )
    assignments = {
        (assignment.resident_id, week): assignment
        for assignment in (schedule.assignments if schedule else [])
        for week in assignment.weeks
    }
    start = instance.calendar.first_week_start
    policy = instance.clinic_policy
    weekdays = clinic_weekdays(instance, view=view, schedule=schedule)
    weeks = visible_week_numbers(
        start,
        instance.calendar.weeks,
        show_past_weeks=show_past_weeks,
        today=today,
    )
    detail_templates: DetailTemplates = {}
    capacity_view = schedule_capacity_view(instance, schedule)
    calendar = "".join(
        _calendar_week_html(
            instance, board, week, site, assignments, attending_board, weekdays, detail_templates,
            capacity_view,
        )
        for week in weeks
    )
    shared_details = "".join(
        '<template class="rbs-clinic-detail-source" hidden '
        f'data-clinic-detail="{key}"><dl>'
        + "".join(_detail_row_html(label, value) for label, value in details)
        + "</dl></template>"
        for details, key in detail_templates.items()
    )
    legend = render_clinic_legend_html(policy, view=view) if show_legend else ""
    empty = ""
    if attending_board is not None and not any(
        people for (week, _day, _session), people in attending_board.items() if week in weeks
    ):
        empty = '<p class="rbs-type-secondary rbs-text-muted">' + (
            "No attendings yet. Add attendings and configure their week-by-week schedules "
            "in Attendings."
            if not instance.attendings
            else "No attending work is scheduled for the selected sites and dates."
        ) + "</p>"
    return (
        f"{legend}{empty}"
        '<div class="rbs-clinic-wrap rbs-clinic-calendar-wrap">'
        f'<div class="rbs-clinic-calendar" role="grid" '
        f'style="--rbs-clinic-days:{len(weekdays)}">{calendar}</div>{shared_details}</div>'
    )


def _calendar_week_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    site: str | None,
    assignments: dict[tuple[str, int], Assignment],
    attending_board: dict[tuple[int, Weekday, Session], list[AttendingOccupant]] | None,
    weekdays: tuple[Weekday, ...],
    detail_templates: DetailTemplates,
    capacity_view: SolverProblem,
) -> str:
    monday = week_monday(instance.calendar.first_week_start, week)
    days = "".join(
        _calendar_day_html(
            instance,
            board,
            week,
            weekday,
            monday + timedelta(days=list(Weekday).index(weekday)),
            site,
            assignments,
            attending_board,
            detail_templates,
            capacity_view,
        )
        for weekday in weekdays
    )
    final_day = monday + timedelta(days=list(Weekday).index(weekdays[-1]))
    range_label = f"{monday:%b} {monday.day} - {final_day:%b} {final_day.day}"
    return (
        f'<div class="rbs-clinic-week" role="row" '
        f'aria-label="{html.escape(range_label)}">{days}</div>'
    )


def _calendar_day_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    weekday: Weekday,
    calendar_day: date,
    site: str | None,
    assignments: dict[tuple[str, int], Assignment],
    attending_board: dict[tuple[int, Weekday, Session], list[AttendingOccupant]] | None,
    detail_templates: DetailTemplates,
    capacity_view: SolverProblem,
) -> str:
    closure = clinic_closure_view(instance.clinic_policy, calendar_day, site)
    if attending_board is not None:
        sessions = "".join(
            _attending_session_html(
                instance, attending_board[(week, weekday, session)], calendar_day, session, closure,
                detail_templates,
            )
            for session in Session
        )
    elif closure.all_selected_sites_closed:
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
                assignments,
                detail_templates,
                capacity_view,
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
        f'<time datetime="{calendar_day.isoformat()}">'
        f"{calendar_day:%b} {calendar_day.day}</time>"
        "</header>"
        f'<div class="rbs-clinic-day-sessions">{sessions}</div></section>'
    )


def _attending_session_html(
    instance: SchedulerInput,
    people: list[AttendingOccupant],
    calendar_day: date,
    session: Session,
    closure: ClinicClosureView,
    detail_templates: DetailTemplates,
) -> str:
    session_label = SESSION_SHORT[session]
    title = html.escape(" · ".join(
        [f"{calendar_day:%b} {calendar_day.day} · {session_label}",
         *[person.label() for person in people]],
    ))
    names = "".join(_attending_html(instance, person, detail_templates) for person in people)
    if not names and closure.all_selected_sites_closed:
        names = "Closed"
    return (
        f'<section class="rbs-clinic-session" title="{title}">'
        '<div class="rbs-clinic-session-heading">'
        f'<span class="rbs-clinic-session-label">{session_label}</span></div>'
        f'<div class="rbs-clinic-session-body">{names}</div></section>'
    )


def _attending_html(
    instance: SchedulerInput,
    person: AttendingOccupant,
    detail_templates: DetailTemplates,
) -> str:
    classes = "rbs-clinic-person rbs-clinic-attending"
    style = ""
    if person.site:
        site = instance.clinic_policy.site(person.site)
        classes += " site"
        style = f' style="{_site_style(site.color, site.light_color)}"'
    elif person.work_type is AttendingWorkType.ADMIN_TIME:
        classes += " admin"
    details = [("Attending", person.name), ("Scheduled", person.work_label())]
    if person.description:
        details.append(("Description", person.description))
    detail_key = detail_templates.setdefault(tuple(details), len(detail_templates))
    return (
        f'<div class="{classes}"{style} tabindex="0" '
        f'data-clinic-detail="{detail_key}" '
        f'aria-label="{html.escape(person.label(), quote=True)}; attending details">'
        f"{_name_html(person.name)}"
        f'<span class="rbs-clinic-attending-work">{html.escape(person.work_label())}</span>'
        '</div>'
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


def _calendar_session_html(
    instance: SchedulerInput,
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]],
    week: int,
    weekday: Weekday,
    session: Session,
    calendar_day: date,
    site: str | None,
    assignments: dict[tuple[str, int], Assignment],
    detail_templates: DetailTemplates,
    capacity_view: SolverProblem,
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
            f'<section class="rbs-clinic-session academic" '
            f'title="{html.escape(title)}">'
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
    names = "".join(
        _person_html(
            person,
            clinic_details(
                instance,
                assignments.get((person.resident_id, week)),
                person,
                week,
                weekday,
                session,
                capacity_view=capacity_view,
            ),
            detail_templates,
            week=week,
        )
        for person in people
    )
    attending_details = []
    attending_markers = []
    sites = (site,) if site is not None else policy.site_ids
    for clinic_site in sites:
        site_config = policy.site(clinic_site)
        needed = policy.attendings_needed(
            site_capacity_points(people, clinic_site),
            clinic_site,
        )
        count = needed
        if site_config.staffing_mode is ClinicStaffingMode.ATTENDING_MANAGED:
            count = capacity_view.clinic_attending_count_on(clinic_site, calendar_day, session)
            maximum = capacity_view.clinic_max_capacity_on(clinic_site, calendar_day, session)
            detail = (
                f"{site_config.name}: {count} preceptor{'s' if count != 1 else ''} scheduled"
                f" · {maximum} capacity points · {needed} needed"
            )
        else:
            detail = (
                f"{needed} attending{'s' if needed != 1 else ''} at {site_config.name}"
            )
        if count or needed:
            attending_details.append(detail)
            attending_markers.append(
                '<span class="rbs-clinic-session-att" '
                f'title="{html.escape(detail, quote=True)}" '
                f'aria-label="{html.escape(detail, quote=True)}" '
                f'style="--rbs-clinic-att-color:{site_config.color}">'
                f"{count} {html.escape(site_config.name)}</span>"
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
        f'<div class="rbs-clinic-session-body">{special_event_html}{names}</div>'
        "</section>"
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


def render_clinic_legend_html(
    policy: ClinicPolicy, *, view: ClinicScheduleView = "residents",
) -> str:
    swatches = "".join(
        '<span class="rbs-clinic-swatch site" '
        f'style="{_site_style(site.color, site.light_color)}">'
        f"{html.escape(site.name)}</span>"
        for site in policy.sites
    )
    if view == "attendings":
        swatches += "".join(
            '<span class="rbs-clinic-swatch'
            f'{" admin" if kind is AttendingWorkType.ADMIN_TIME else ""}">'
            f"{html.escape(label)}</span>"
            for kind, label in ATTENDING_WORK_TYPE_LABELS.items()
        )
    else:
        swatches += '<span class="rbs-clinic-swatch admin">Admin</span>'
        swatches += '<span class="rbs-clinic-swatch special-event">Special event</span>'
    return (
        '<div class="rbs-clinic-legend">'
        f'<span class="rbs-clinic-key-label">Key</span>{swatches}</div>'
    )


def _person_html(
    person: ClinicOccupant,
    details: list[tuple[str, str]],
    detail_templates: DetailTemplates,
    *,
    week: int,
) -> str:
    classes = "rbs-clinic-person"
    if person.admin:
        classes += " admin"
    elif person.site is not None:
        classes += " site"
    style = ""
    if person.site_color and person.site_light_color:
        style = f' style="{_site_style(person.site_color, person.site_light_color)}"'
    detail_key = detail_templates.setdefault(tuple(details), len(detail_templates))
    return (
        f'<div class="{classes}"{style} tabindex="0" '
        f'data-resident-id="{html.escape(person.resident_id, quote=True)}" '
        f'data-start-week="{week}" '
        f'data-clinic-detail="{detail_key}" '
        f'aria-label="{html.escape(person.label())}; clinic details">'
        f"{_display_label_html(person)}"
        '</div>'
    )


def _detail_row_html(label: str, value: str) -> str:
    content = html.escape(value)
    if label == "Allowed options" and value != "None":
        content = '<span class="rbs-clinic-detail-options">' + "".join(
            f'<span class="rbs-clinic-detail-option">{html.escape(option)}</span>'
            for option in value.split(", ")
        ) + "</span>"
    elif label == "Scheduled":
        content = f'<span class="rbs-clinic-detail-site">{content}</span>'
    elif label in {"Restrictions", "Placement"}:
        content = f'<span class="rbs-clinic-detail-notice">{content}</span>'
    return f"<dt>{html.escape(label)}</dt><dd>{content}</dd>"


def _site_style(color: str, light_color: str) -> str:
    return f"--rbs-clinic-site-color:{color};--rbs-clinic-site-tint:{light_color}"


def _display_label_html(person: ClinicOccupant) -> str:
    training_level = html.escape(
        person.training_level_code or default_training_level_code(person.pgy)
    )
    return (
        f'<span class="rbs-clinic-training-level">{training_level}</span> '
        f'{_name_html(person.name)}'
    )


def _name_html(name: str) -> str:
    name_parts = name.rsplit(" ", 1)
    if len(name_parts) == 1:
        name_html = f'<strong class="rbs-clinic-last-name">{html.escape(name)}</strong>'
    else:
        given_names, last_name = name_parts
        name_html = (
            f"{html.escape(given_names)} "
            f'<strong class="rbs-clinic-last-name">{html.escape(last_name)}</strong>'
        )
    return name_html
