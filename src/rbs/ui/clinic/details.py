"""Read-only explanations of the rules behind a clinic occurrence."""

from __future__ import annotations

from rbs.models.clinic import ClinicStaffingMode, clinic_slot_date
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput, SolverProblem
from rbs.models.schedule import Assignment
from rbs.ui.clinic.projection import ClinicOccupant


def clinic_details(
    instance: SchedulerInput,
    assignment: Assignment | None,
    person: ClinicOccupant,
    week: int,
    weekday: Weekday,
    session: Session,
    *,
    capacity_view: SolverProblem | None = None,
) -> list[tuple[str, str]]:
    """Describe current input rules, without claiming whole-schedule feasibility."""
    resident = instance.residents_by_id[person.resident_id]
    day = clinic_slot_date(instance.calendar.first_week_start, week, weekday)
    rows = [
        ("Resident", f"{instance.training_level_label(resident.pgy, compact=True)} {resident.name}")
    ]
    rotation = None
    if assignment is not None:
        rotation = instance.rotations_by_id.get(assignment.rotation_id)
    rows.append(("Scheduled", "Admin" if person.admin else person.site_name or "Clinic"))
    rows.append(("Rotation", rotation.name if rotation else "No rotation available"))
    if person.manual_override:
        rows.append(("Placement", "Manual override"))
    if person.admin:
        rows.append(("Clinic attendance", "Reserved for Admin in this half-day"))
    elif person.site is not None:
        site = instance.clinic_policy.site(person.site)
        if site.staffing_mode is ClinicStaffingMode.ATTENDING_MANAGED:
            capacity = capacity_view or instance
            preceptors = capacity.clinic_attending_count_on(site.id, day, session)
            maximum = capacity.clinic_max_capacity_on(site.id, day, session)
            rows.extend([
                ("Preceptors", f"{preceptors} scheduled at {site.name}"),
                ("Clinic capacity", f"{maximum} capacity points"),
            ])

    reasons = []
    if week in resident.vacation_weeks:
        reasons.append("Vacation this week")
    if day in resident.days_off:
        reasons.append("Individual day off")
    reasons.extend(
        special.name
        for special in instance.special_rotations_for_resident(
            resident.id,
            calendar_day=day,
            session=session,
        )
    )
    if rotation and rotation.away:
        reasons.append("Away rotation")
    if instance.is_academic_half_day(week, weekday, session):
        reasons.append("Academic Half Day")
    if reasons:
        rows.append(("Restrictions", "; ".join(reasons)))

    policy = instance.clinic_policy
    options: set[tuple[Weekday, Session]] = set()
    if rotation and not rotation.clinic_hours_disabled and rotation.clinic:
        options = {
            (slot.weekday, slot.session)
            for slot in rotation.clinic.slots
            if slot.weekday is not None
            and slot.session is not None
            and not instance.is_academic_half_day(week, slot.weekday, slot.session)
        }
    rows.append(("Allowed options", _allowed_options_label(options)))
    recurring = next(
        (
            slot
            for slot in resident.clinic_half_days
            if (slot.weekday, slot.session) == (weekday, session)
        ),
        None,
    )
    if recurring:
        rows.append(
            (
                "Recurring clinic",
                "Recurring at "
                + ", ".join(
                    policy.site_name(site)
                    for site in (policy.resolve_site_ids(recurring.sites) or policy.site_ids)
                ),
            )
        )
    return rows


def _allowed_options_label(options: set[tuple[Weekday, Session]]) -> str:
    day_labels = {
        Weekday.MONDAY: "M",
        Weekday.TUESDAY: "T",
        Weekday.WEDNESDAY: "W",
        Weekday.THURSDAY: "Th",
        Weekday.FRIDAY: "F",
        Weekday.SATURDAY: "Sa",
        Weekday.SUNDAY: "Su",
    }
    labels = []
    for weekday, label in day_labels.items():
        sessions = {session for day, session in options if day == weekday}
        if len(sessions) == 2:
            labels.append(label)
        elif sessions:
            suffix = "AM" if Session.MORNING in sessions else "PM"
            labels.append(f"{label} {suffix}")
    return ", ".join(labels) or "None"
