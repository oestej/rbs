"""Pure clinic-schedule queries shared by screens, exports, and editors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rbs.models.clinic import ClinicPolicy, clinic_slot_date
from rbs.models.curriculum import default_training_level_code
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
from rbs.models.instance import SchedulerInput
from rbs.models.resident import Resident
from rbs.models.schedule import Assignment, Schedule
from rbs.models.special import SpecialRotation, SpecialRotationKind

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
    """Return the weekdays needed by the clinic calendar projection."""
    days = set(WEEKDAYS_MF)
    if instance is not None:
        for site in instance.clinic_policy.sites:
            days.update(half_day.weekday for half_day in site.half_days)
            days.update(
                tuple(Weekday)[override.date.weekday()]
                for override in site.capacity_overrides
            )
        for rotation in instance.rotations:
            if rotation.clinic is not None:
                days.update(
                    slot.weekday
                    for slot in rotation.clinic.slots
                    if slot.weekday is not None
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
    return (weekday, session) == policy.recurring_academic_half_day


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
    """Return full/partial closure state for the selected clinic sites."""
    closure = policy.closure_on(calendar_day)
    if closure is None:
        return ClinicClosureView()
    selected_sites = (site,) if site is not None else policy.site_ids
    configured_closed = set(closure.sites)
    closed_ids = tuple(
        site_id for site_id in selected_sites if site_id in configured_closed
    )
    if not closed_ids:
        return ClinicClosureView()
    return ClinicClosureView(
        closed_site_ids=closed_ids,
        closed_site_names=tuple(policy.site_name(site_id) for site_id in closed_ids),
        name=closure.name,
        all_selected_sites_closed=len(closed_ids) == len(selected_sites),
    )


def occupancy(
    instance: SchedulerInput,
    schedule: Schedule | None,
) -> dict[tuple[int, Weekday, Session], list[ClinicOccupant]]:
    """Project resident attendance into each academic-week half-day."""
    policy = instance.clinic_policy
    board: dict[tuple[int, Weekday, Session], list[ClinicOccupant]] = {
        (week, day, session): []
        for week in range(1, instance.calendar.weeks + 1)
        for day, session in half_days(instance)
    }
    if schedule is None or schedule.is_empty():
        return board

    residents = instance.residents_by_id
    seen: dict[tuple[int, Weekday, Session], set[str]] = {
        key: set() for key in board
    }
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
            if key not in board or occupant.resident_id in seen[key]:
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


def occupants_for_site(
    people: list[ClinicOccupant],
    site: str | None,
) -> list[ClinicOccupant]:
    """Return occupants visible in an all-sites or single-site view."""
    if site is None:
        return people
    return [person for person in people if occupant_site(person) == site]


def calendar_occupants(
    people: list[ClinicOccupant],
    policy: ClinicPolicy,
) -> list[ClinicOccupant]:
    """Order a calendar session by site, training level, and last name."""
    site_order = {site_id: index for index, site_id in enumerate(policy.site_ids)}

    def sort_key(person: ClinicOccupant) -> tuple:
        last_name = person.name.rsplit(" ", 1)[-1]
        site_index = (
            len(site_order)
            if person.admin
            else site_order.get(person.site or "", 999)
        )
        return (
            site_index,
            person.training_level_order
            if person.training_level_order is not None
            else person.pgy,
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
    """Return peak half-day attendings and total attending-sessions."""
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
    """Attending-sessions per week at one site or the primary site."""
    board = occupancy(instance, schedule)
    policy = instance.clinic_policy
    selected_site = site or policy.primary_site_id
    by_week = {
        week: 0 for week in range(1, instance.calendar.weeks + 1)
    }
    for (week, _weekday, _session), people in board.items():
        by_week[week] += policy.attendings_needed(
            site_headcount(people, selected_site),
            selected_site,
        )
    return by_week


def special_events_for_slot(
    instance: SchedulerInput,
    calendar_day: date,
    session: Session,
) -> tuple[SpecialRotation, ...]:
    """Return Clinic Calendar events occupying one dated half-day."""
    return tuple(
        special
        for special in instance.special_rotations
        if special.kind is SpecialRotationKind.EVENT
        and special.blocks(calendar_day, session)
    )


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
        templates = {
            (slot.weekday, slot.session): slot for slot in assignment.clinic_slots
        }
        admin_keys = {
            (slot.weekday, slot.session)
            for slot in assignment.clinic_slots
            if slot.admin
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
                            manual_override=bool(
                                template and template.manual_override
                            ),
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
