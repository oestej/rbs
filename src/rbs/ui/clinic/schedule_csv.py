"""Tabular CSV export for the overall program clinic schedule."""

from __future__ import annotations

import re
from datetime import date

from rbs.models.clinic import clinic_slot_date
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.ui.clinic.projection import (
    ACADEMIC_LABEL,
    SESSION_SHORT,
    WEEKDAY_SHORT,
    ClinicScheduleView,
    attending_occupancy,
    clinic_closure_view,
    half_days,
    is_academic_week,
    occupancy,
    occupants_for_site,
    site_capacity_points,
    special_events_for_slot,
)
from rbs.ui.csv_export import build_spreadsheet_csv
from rbs.ui.schedule_projection import visible_week_numbers, week_monday


def clinic_schedule_csv_columns(
    instance: SchedulerInput,
    *,
    view: ClinicScheduleView = "residents",
    schedule: Schedule | None = None,
) -> tuple[tuple[str, str], ...]:
    """Columns for every weekday shown by the configured Clinic calendar."""
    return (
        ("week", "Academic Week"),
        ("week_of", "Week Of"),
        *(
            (
                _slot_key(weekday.value, session.value),
                f"{WEEKDAY_SHORT[weekday]} {SESSION_SHORT[session]}",
            )
            for weekday, session in half_days(instance, view=view, schedule=schedule)
        ),
    )


def clinic_schedule_csv_rows(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
    view: ClinicScheduleView = "residents",
) -> list[dict[str, str]]:
    """Return the visible clinic schedule as one flat row per academic week."""
    board = occupancy(instance, schedule) if view == "residents" else {}
    attending_board = (
        attending_occupancy(instance, schedule, site=site) if view == "attendings" else None
    )
    policy = instance.clinic_policy
    visible_sites = (site,) if site is not None else policy.site_ids
    weeks = visible_week_numbers(
        instance.calendar.first_week_start,
        instance.calendar.weeks,
        show_past_weeks=show_past_weeks,
        today=today,
    )
    rows: list[dict[str, str]] = []
    for week in weeks:
        monday = week_monday(instance.calendar.first_week_start, week)
        row = {
            "week": str(week),
            "week_of": _date_label(monday),
        }
        for weekday, session in half_days(instance, view=view, schedule=schedule):
            key = _slot_key(weekday.value, session.value)
            calendar_day = clinic_slot_date(
                instance.calendar.first_week_start,
                week,
                weekday,
            )
            closure = clinic_closure_view(policy, calendar_day, site)
            if attending_board is not None:
                row[key] = "\n".join([
                    *([closure.label()] if closure.is_closed else []),
                    *[person.label() for person in attending_board[(week, weekday, session)]],
                ])
                continue
            event_labels = [
                f"{special.name}: "
                + ", ".join(
                    f"{instance.training_level_label(resident.pgy, compact=True)} {resident.name}"
                    for resident_id in special.resident_ids
                    for resident in (instance.residents_by_id[resident_id],)
                )
                for special in special_events_for_slot(instance, calendar_day, session)
            ]
            if closure.all_selected_sites_closed:
                row[key] = "\n".join([closure.label(), *event_labels])
                continue
            if is_academic_week(instance, week, weekday, session):
                row[key] = "\n".join(
                    [
                        *([closure.label()] if closure.is_partial else []),
                        ACADEMIC_LABEL,
                        *event_labels,
                    ]
                )
                continue
            people = occupants_for_site(board[(week, weekday, session)], site)
            labels = [
                person.display_label() if site is not None else person.label() for person in people
            ]
            attending_labels = []
            for clinic_site in visible_sites:
                needed = policy.attendings_needed(
                    site_capacity_points(people, clinic_site),
                    clinic_site,
                )
                if needed:
                    noun = "attending" if needed == 1 else "attendings"
                    attending_labels.append(f"{needed} {noun} - {policy.site_name(clinic_site)}")
            row[key] = "\n".join(
                [
                    *([closure.label()] if closure.is_partial else []),
                    *event_labels,
                    *labels,
                    *attending_labels,
                ]
            )
        rows.append(row)
    return rows


def build_clinic_schedule_csv(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
    view: ClinicScheduleView = "residents",
) -> str:
    """Build a spreadsheet-friendly CSV matching the former clinic sheet."""
    columns = clinic_schedule_csv_columns(instance, view=view, schedule=schedule)
    rows = clinic_schedule_csv_rows(
        instance,
        schedule,
        show_past_weeks=show_past_weeks,
        today=today,
        site=site,
        view=view,
    )
    return build_spreadsheet_csv(
        [label for _field, label in columns],
        ([row.get(field, "") for field, _label in columns] for row in rows),
    )


def clinic_schedule_csv_filename(
    academic_year: str,
    *,
    site: str | None,
    exported_on: date | None = None,
    view: ClinicScheduleView = "residents",
) -> str:
    year_slug = re.sub(r"[^0-9a-z]+", "-", academic_year.lower()).strip("-")
    site_slug = site.replace("_", "-") if site is not None else "all-sites"
    export_date = (exported_on or date.today()).isoformat()
    prefix = "attending-schedule" if view == "attendings" else "clinic-schedule"
    return f"{prefix}-{year_slug}-{site_slug}-exported-{export_date}.csv"


def _slot_key(weekday: str, session: str) -> str:
    return f"{weekday}_{session}"


def _date_label(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"
