"""Site choices attached to a resident's configured clinic half-day."""

from datetime import date

from rbs.models.enums import Session, Weekday
from rbs.models.resident import Resident
from rbs.models.rotation import Rotation


def clinic_directory_is_open(problem, calendar_day: date, session: Session) -> bool:
    """Whether any clinic operates this half-day, before staffing is assigned.

    A directory-wide closed half-day cancels clinic, as a dated closure does.
    A staffed seat at an unrelated clinic cannot cancel a restricted session
    or make its allowed clinics usable.
    """
    return any((problem.clinic_max_attendings_on(site, calendar_day, session) > 0
                or problem.clinic_max_capacity_on(site, calendar_day, session) > 0)
               for site in problem.clinic_policy.site_ids)


def declared_clinic_sites(
    resident: Resident, rotation: Rotation, weekday: Weekday, session: Session,
) -> list[str]:
    """Continuity sessions take precedence over a rotation's overlapping template."""
    for slot in resident.clinic_half_days:
        if slot.weekday is weekday and slot.session is session:
            return slot.sites
    if rotation.clinic is not None and not rotation.clinic_hours_disabled:
        for slot in rotation.clinic.slots:
            if slot.weekday is weekday and slot.session is session:
                return slot.sites
    return []
