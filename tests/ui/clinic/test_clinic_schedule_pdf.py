from datetime import date, timedelta
from io import BytesIO

from pypdf import PdfReader

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
from rbs.ui.clinic.schedule_pdf import (
    build_clinic_schedule_pdf,
    clinic_schedule_pdf_filename,
)


def _schedule_for_export() -> tuple[object, Schedule, object, object]:
    instance = sample_instance()
    maple_resident = instance.residents[0]
    cedar_resident = instance.residents[2]
    schedule = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=51,
                end_week=51,
                weeks=[51],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.TUESDAY,
                        session=Session.MORNING,
                        site=site,
                        week=51,
                    )
                ],
            )
            for resident, site in (
                (maple_resident, "maple"),
                (cedar_resident, "cedar"),
            )
        ],
    )
    return instance, schedule, maple_resident, cedar_resident


def test_clinic_schedule_pdf_uses_dated_calendar_cards_and_site_filter() -> None:
    instance, schedule, maple_resident, cedar_resident = _schedule_for_export()
    today = instance.calendar.first_week_start + timedelta(weeks=50)

    pdf = build_clinic_schedule_pdf(
        instance,
        schedule,
        show_past_weeks=False,
        today=today,
        site="maple",
    )

    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert reader.metadata.title == "Clinic Schedule - Maple"
    assert "Clinic Schedule" in text
    assert "2026-2027 | Maple | Current and future dates" in text
    assert "MONDAY" in text and "JUN 14" in text
    assert "TUESDAY" in text and "JUN 15" in text
    assert "Academic Half Day" in text
    assert "Academic Week" not in text
    assert "Week Of" not in text
    assert maple_resident.name in text
    assert cedar_resident.name not in text
    assert "1 Maple" in text
    assert f"Maple: PGY{maple_resident.pgy} {maple_resident.name}" in text
    assert "MPL:" not in text
    assert "ATT" not in text
    assert "Page 1" in text


def test_clinic_schedule_pdf_filename_identifies_the_site() -> None:
    assert (
        clinic_schedule_pdf_filename(
            "2026-2027",
            site=None,
            exported_on=date(2026, 8, 23),
        )
        == "clinic-schedule-2026-2027-all-sites-exported-2026-08-23.pdf"
    )
    assert (
        clinic_schedule_pdf_filename(
            "2026-2027",
            site="maple",
            exported_on=date(2026, 8, 23),
        )
        == "clinic-schedule-2026-2027-maple-exported-2026-08-23.pdf"
    )


def test_clinic_schedule_pdf_applies_week_specific_academic_override() -> None:
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
    today = instance.calendar.first_week_start + timedelta(weeks=50)

    pdf = build_clinic_schedule_pdf(
        instance,
        schedule,
        show_past_weeks=False,
        today=today,
    )
    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )
    tuesday = text.index("TUESDAY")
    wednesday = text.index("WEDNES", tuesday)
    thursday = text.index("THURS", wednesday)

    assert "Academic Half Day" in text[tuesday:wednesday]
    assert "Academic Half Day" not in text[wednesday:thursday]


def test_clinic_schedule_pdf_marks_full_closure_days() -> None:
    instance = sample_instance()
    christmas_week = instance.calendar.first_week_start + timedelta(weeks=25)

    pdf = build_clinic_schedule_pdf(
        instance,
        schedule=None,
        show_past_weeks=False,
        today=christmas_week,
    )

    text = "\n".join(
        page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
    )
    assert "DEC 25" in text
    assert "Christmas - Closed" in text
