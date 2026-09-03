import csv
from datetime import date, timedelta
from io import StringIO

from rbs.catalog import sample_instance
from rbs.models.enums import (
    RotationKind,
    Session,
    SolverEngineName,
    SolverStatus,
    Weekday,
)
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
from rbs.ui.clinic.schedule_csv import (
    build_clinic_schedule_csv,
    clinic_schedule_csv_filename,
)


def _schedule_for_export() -> tuple[object, Schedule, object, object, object]:
    instance = sample_instance()
    maple_resident = instance.residents[0]
    cedar_resident = instance.residents[2]
    admin_resident = instance.residents[3]
    assignments = []
    for resident, site, weekday, admin in (
        (maple_resident, "maple", Weekday.TUESDAY, False),
        (cedar_resident, "cedar", Weekday.TUESDAY, False),
        (admin_resident, None, Weekday.MONDAY, True),
    ):
        assignments.append(
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=51,
                end_week=51,
                weeks=[51],
                clinic_slots=[
                    AssignedClinic(
                        weekday=weekday,
                        session=Session.MORNING,
                        site=site,
                        admin=admin,
                        week=51,
                    )
                ],
            )
        )
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=assignments,
    )
    return instance, schedule, maple_resident, cedar_resident, admin_resident


def test_clinic_schedule_csv_keeps_week_by_half_day_structure() -> None:
    instance, schedule, maple_resident, cedar_resident, admin_resident = (
        _schedule_for_export()
    )
    today = instance.calendar.first_week_start + timedelta(weeks=50)

    all_csv = build_clinic_schedule_csv(
        instance,
        schedule,
        show_past_weeks=False,
        today=today,
    )
    all_rows = list(csv.DictReader(StringIO(all_csv)))
    week_51 = next(row for row in all_rows if row["Academic Week"] == "51")
    assert list(all_rows[0]) == [
        "Academic Week",
        "Week Of",
        "Mon AM",
        "Mon PM",
        "Tue AM",
        "Tue PM",
        "Wed AM",
        "Wed PM",
        "Thu AM",
        "Thu PM",
        "Fri AM",
        "Fri PM",
    ]
    assert maple_resident.name in week_51["Tue AM"]
    assert cedar_resident.name in week_51["Tue AM"]
    assert "1 attending - Maple" in week_51["Tue AM"]
    assert "1 attending - Cedar" in week_51["Tue AM"]
    assert admin_resident.name in week_51["Mon AM"]
    assert "Admin" in week_51["Mon AM"]

    maple_csv = build_clinic_schedule_csv(
        instance,
        schedule,
        show_past_weeks=False,
        today=today,
        site="maple",
    )
    maple_rows = list(csv.DictReader(StringIO(maple_csv)))
    maple_week_51 = next(row for row in maple_rows if row["Academic Week"] == "51")
    assert maple_resident.name in maple_week_51["Tue AM"]
    assert cedar_resident.name not in maple_week_51["Tue AM"]
    assert admin_resident.name not in maple_week_51["Mon AM"]
    assert maple_week_51["Wed PM"] == "Academic Half Day"


def test_clinic_schedule_csv_filename_identifies_the_site() -> None:
    assert (
        clinic_schedule_csv_filename(
            "2026-2027",
            site=None,
            exported_on=date(2026, 8, 23),
        )
        == "clinic-schedule-2026-2027-all-sites-exported-2026-08-23.csv"
    )
    assert (
        clinic_schedule_csv_filename(
            "2026-2027",
            site="maple",
            exported_on=date(2026, 8, 23),
        )
        == "clinic-schedule-2026-2027-maple-exported-2026-08-23.csv"
    )


def test_clinic_schedule_csv_applies_week_specific_academic_override() -> None:
    instance, schedule, *_residents = _schedule_for_export()
    raw = instance.model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {
            "week": 51,
            "weekday": Weekday.TUESDAY.value,
            "session": Session.AFTERNOON.value,
        }
    ]
    instance = SchedulerInput.model_validate(raw)

    rows = list(
        csv.DictReader(
            StringIO(build_clinic_schedule_csv(instance, schedule))
        )
    )
    week_51 = next(row for row in rows if row["Academic Week"] == "51")

    assert week_51["Tue PM"] == "Academic Half Day"
    assert week_51["Wed PM"] == ""


def test_clinic_schedule_csv_marks_full_closure_days() -> None:
    instance = sample_instance()

    rows = list(
        csv.DictReader(
            StringIO(build_clinic_schedule_csv(instance, schedule=None))
        )
    )
    christmas_week = next(row for row in rows if row["Academic Week"] == "26")

    assert christmas_week["Fri AM"] == "Christmas · Closed"
    assert christmas_week["Fri PM"] == "Christmas · Closed"
