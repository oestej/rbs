"""Tabular CSV export for the overall program clinic schedule."""

from __future__ import annotations

import csv
import re
from datetime import date
from io import StringIO

from rbs.models.clinic import clinic_slot_date
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import Schedule
from rbs.ui.clinic.board import (
    ACADEMIC_LABEL,
    SESSION_SHORT,
    WEEKDAY_SHORT,
    clinic_closure_view,
    half_days,
    is_academic_week,
    occupancy,
    occupants_for_site,
    site_headcount,
    special_events_for_slot,
)
from rbs.ui.grid import visible_week_numbers, week_monday


def clinic_schedule_csv_columns(
    instance: SchedulerInput,
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
            for weekday, session in half_days(instance)
        ),
    )


def clinic_schedule_csv_rows(
    instance: SchedulerInput,
    schedule: Schedule | None,
    *,
    show_past_weeks: bool = True,
    today: date | None = None,
    site: str | None = None,
) -> list[dict[str, str]]:
    """Return the visible clinic schedule as one flat row per academic week."""
    board = occupancy(instance, schedule)
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
        for weekday, session in half_days(instance):
            key = _slot_key(weekday.value, session.value)
            calendar_day = clinic_slot_date(
                instance.calendar.first_week_start,
                week,
                weekday,
            )
            closure = clinic_closure_view(policy, calendar_day, site)
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
                    site_headcount(people, clinic_site),
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
) -> str:
    """Build a spreadsheet-friendly CSV matching the former clinic sheet."""
    columns = clinic_schedule_csv_columns(instance)
    rows = clinic_schedule_csv_rows(
        instance,
        schedule,
        show_past_weeks=show_past_weeks,
        today=today,
        site=site,
    )
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([label for _field, label in columns])
    for row in rows:
        writer.writerow([row.get(field, "") for field, _label in columns])
    return output.getvalue()


def clinic_schedule_csv_filename(
    academic_year: str,
    *,
    site: str | None,
    exported_on: date | None = None,
) -> str:
    year_slug = re.sub(r"[^0-9a-z]+", "-", academic_year.lower()).strip("-")
    site_slug = site.replace("_", "-") if site is not None else "all-sites"
    export_date = (exported_on or date.today()).isoformat()
    return f"clinic-schedule-{year_slug}-{site_slug}-exported-{export_date}.csv"


def _slot_key(weekday: str, session: str) -> str:
    return f"{weekday}_{session}"


def _date_label(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"
