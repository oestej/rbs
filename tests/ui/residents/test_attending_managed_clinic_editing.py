"""Resident clinic edits see the preceptors a solve placed at a clinic."""

from datetime import timedelta

from rbs.catalog import sample_instance
from rbs.models.attending import AttendingWorkType
from rbs.models.enums import RotationKind, Session, SolverEngineName, SolverStatus, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.schedule import AssignedAttendingWork, Assignment, Schedule, ScheduleMeta
from rbs.ui.residents.ops import resident_clinic_available_site_ids


def test_a_resident_can_move_to_a_clinic_the_solve_staffed() -> None:
    raw = sample_instance().model_dump(mode="json")
    maple = next(site for site in raw["clinic_policy"]["sites"] if site["id"] == "maple")
    maple["staffing_mode"] = "attending_managed"
    instance = SchedulerInput.model_validate(raw)
    week = 2
    tuesday = instance.calendar.first_week_start + timedelta(weeks=week - 1, days=1)
    attending = next(
        attending
        for attending in instance.attendings
        if attending.half_days_per_week
        and attending.is_scheduled_on(
            tuesday,
            academic_year_start=instance.calendar.first_week_start,
            academic_year_end=instance.calendar.first_week_start
            + timedelta(weeks=instance.calendar.weeks, days=-1),
        )
        and not attending.is_on_vacation(tuesday)
    )
    resident = instance.residents[0]
    unstaffed = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[
            Assignment(
                resident_id=resident.id,
                rotation_id="clinic",
                kind=RotationKind.CLINIC,
                start_week=week,
                end_week=week,
                weeks=[week],
            )
        ],
    )
    staffed = unstaffed.model_copy(
        update={
            "attending_work": [
                AssignedAttendingWork(
                    attending_id=attending.id,
                    week=week,
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    work_type=AttendingWorkType.PRECEPTING_CLINIC,
                    clinic_id="maple",
                )
            ]
        }
    )
    target = {
        "resident_id": resident.id,
        "week": week,
        "weekday": Weekday.TUESDAY,
        "session": Session.MORNING,
    }

    assert "maple" not in resident_clinic_available_site_ids(instance, unstaffed, **target)
    assert "maple" in resident_clinic_available_site_ids(instance, staffed, **target)
