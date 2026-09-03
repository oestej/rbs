from collections import defaultdict
from datetime import date

from rbs.catalog import sample_instance
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.rotation import ClinicPolicy
from rbs.models.schedule import AssignedClinic, Assignment
from rbs.solver.core.clinic_allocation import (
    _Candidate,
    _remainder_assignment_key,
    assign_clinic_sites,
    clinic_weekly_attendings,
)


def _slot(week: int, weekday: Weekday, session: Session) -> AssignedClinic:
    return AssignedClinic(weekday=weekday, session=session, week=week)


def _secondary_site_id(instance) -> str:
    return next(
        site_id
        for site_id in instance.clinic_policy.site_ids
        if site_id != instance.clinic_policy.primary_site_id
    )


def _maximum_site_capacity(instance, site_id: str) -> int:
    site = instance.clinic_policy.site(site_id)
    return max(
        (
            half_day.max_residents(site.residents_per_attending)
            for half_day in site.half_days
        ),
        default=0,
    )


def _with_closure(sites: list[str]):
    instance = sample_instance()
    raw = instance.clinic_policy.model_dump(mode="json")
    raw["closure_days"] = [
        {
            "date": "2026-07-07",
            "name": "Site maintenance",
            "sites": sites,
        }
    ]
    return instance.model_copy(
        update={"clinic_policy": ClinicPolicy.model_validate(raw)}
    )


def test_site_tie_break_prefers_a_residents_existing_am_pm_location() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    slot = _slot(2, Weekday.TUESDAY, Session.AFTERNOON)
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="elective",
        kind=RotationKind.STANDARD,
        start_week=2,
        end_week=2,
        weeks=[2],
        clinic_slots=[slot],
    )
    candidate = _Candidate(
        assignment=assignment,
        slot=slot,
        resident_id=resident.id,
        pgy=resident.pgy,
        clinic_ids=list(instance.clinic_policy.site_ids),
        calendar_day=date(2026, 7, 7),
    )
    primary = instance.clinic_policy.primary_site_id
    secondary = _secondary_site_id(instance)
    filled = defaultdict(int)
    assigned = defaultdict(int)
    assigned_day = defaultdict(int)
    assigned_week = defaultdict(int)
    weekly = defaultdict(int)
    assigned_day[resident.id, 2, Weekday.TUESDAY, secondary] = 1
    assigned_week[resident.id, 2, secondary] = 1

    secondary_key = _remainder_assignment_key(
        candidate,
        secondary,
        instance.clinic_policy,
        {},
        filled,
        assigned,
        assigned_day,
        assigned_week,
        weekly,
    )
    primary_key = _remainder_assignment_key(
        candidate,
        primary,
        instance.clinic_policy,
        {},
        filled,
        assigned,
        assigned_day,
        assigned_week,
        weekly,
    )

    assert secondary_key < primary_key


def test_flex_clinic_is_about_a_quarter_maple() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    # 8 flex half-days: 5 Maple windows + 3 Cedar-only. Target = round(0.25*8) = 2.
    slots = [
        _slot(1, Weekday.MONDAY, Session.MORNING),
        _slot(1, Weekday.MONDAY, Session.AFTERNOON),
        _slot(1, Weekday.TUESDAY, Session.MORNING),
        _slot(1, Weekday.TUESDAY, Session.AFTERNOON),
        _slot(1, Weekday.WEDNESDAY, Session.MORNING),
        _slot(1, Weekday.THURSDAY, Session.MORNING),
        _slot(1, Weekday.THURSDAY, Session.AFTERNOON),
        _slot(1, Weekday.FRIDAY, Session.MORNING),
    ]
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="clinic",
        kind=RotationKind.CLINIC,
        start_week=1,
        end_week=1,
        weeks=[1],
        clinic_slots=slots,
    )
    assign_clinic_sites(instance, [assignment])
    secondary_site = _secondary_site_id(instance)
    secondary_count = sum(1 for slot in slots if slot.site == secondary_site)
    assert secondary_count == 2
    assert all(
        slot.site == instance.clinic_policy.primary_site_id
        for slot in slots
        if instance.clinic_policy.max_capacity(
            secondary_site,
            slot.weekday,
            slot.session,
        )
        == 0
    )


def test_flex_clinic_keeps_the_reference_site_when_targets_allow_it() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta

    instance = sample_instance()
    resident = instance.residents[0]
    positions = [
        (Weekday.MONDAY, Session.MORNING),
        (Weekday.MONDAY, Session.AFTERNOON),
        (Weekday.TUESDAY, Session.MORNING),
        (Weekday.TUESDAY, Session.AFTERNOON),
    ]
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="clinic",
        kind=RotationKind.CLINIC,
        start_week=1,
        end_week=1,
        weeks=[1],
        clinic_slots=[_slot(1, weekday, session) for weekday, session in positions],
    )
    reference_assignment = assignment.model_copy(deep=True)
    secondary = _secondary_site_id(instance)
    for slot in reference_assignment.clinic_slots:
        slot.site = instance.clinic_policy.primary_site_id
    reference_assignment.clinic_slots[-1].site = secondary
    reference = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[reference_assignment],
    )

    assign_clinic_sites(
        instance,
        [assignment],
        reference_schedule=reference,
    )

    assert assignment.clinic_slots[-1].site == secondary
    assert sum(slot.site == secondary for slot in assignment.clinic_slots) == 1


def test_automatic_clinic_lock_keeps_the_prior_site_during_reallocation() -> None:
    from rbs.models.enums import SolverEngineName, SolverStatus
    from rbs.models.schedule import Schedule, ScheduleMeta

    instance = sample_instance().revised(lock_through_today=True)
    resident = instance.residents[0]
    secondary = _secondary_site_id(instance)
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="elective",
        kind=RotationKind.ELECTIVE,
        start_week=1,
        end_week=1,
        weeks=[1],
        clinic_slots=[_slot(1, Weekday.TUESDAY, Session.MORNING)],
    )
    reference_assignment = assignment.model_copy(deep=True)
    reference_assignment.clinic_slots[0].site = secondary
    reference = Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.CP_SAT,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=[reference_assignment],
    )

    assign_clinic_sites(instance, [assignment], reference_schedule=reference)

    assert assignment.clinic_slots[0].site == secondary


def test_partial_closure_assigns_the_open_clinic_site() -> None:
    for closed_sites, expected_site in (
        (["maple"], "cedar"),
        (["cedar"], "maple"),
    ):
        instance = _with_closure(closed_sites)
        assignment = Assignment(
            resident_id=instance.residents[0].id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=2,
            end_week=2,
            weeks=[2],
            clinic_slots=[_slot(2, Weekday.TUESDAY, Session.MORNING)],
        )

        assign_clinic_sites(instance, [assignment])

        assert len(assignment.clinic_slots) == 1
        assert assignment.clinic_slots[0].site == expected_site


def test_full_closure_removes_the_clinic_assignment_for_that_date() -> None:
    instance = _with_closure(["maple", "cedar"])
    assignment = Assignment(
        resident_id=instance.residents[0].id,
        rotation_id="elective",
        kind=RotationKind.STANDARD,
        start_week=2,
        end_week=2,
        weeks=[2],
        clinic_slots=[_slot(2, Weekday.TUESDAY, Session.MORNING)],
    )

    assign_clinic_sites(instance, [assignment])

    assert assignment.clinic_slots == []


def test_metro_pinned_sites_are_not_rebalanced() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="inpatient_peds_metro",
        kind=RotationKind.STANDARD,
        start_week=1,
        end_week=1,
        weeks=[1],
        clinic_slots=[
            AssignedClinic(
                weekday=Weekday.FRIDAY,
                session=Session.MORNING,
                week=1,
                site=instance.clinic_policy.primary_site_id,
            ),
            AssignedClinic(
                weekday=Weekday.FRIDAY,
                session=Session.AFTERNOON,
                week=1,
                site=_secondary_site_id(instance),
            ),
        ],
    )
    assign_clinic_sites(instance, [assignment])
    am, pm = assignment.clinic_slots
    assert am.site == _secondary_site_id(instance)
    assert pm.site == instance.clinic_policy.primary_site_id


def test_maple_capacity_is_respected() -> None:
    instance = sample_instance()
    slots = []
    assignments = []
    for resident in instance.residents[:6]:
        slot = _slot(1, Weekday.TUESDAY, Session.MORNING)
        slots.append(slot)
        assignments.append(
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    slot,
                    _slot(1, Weekday.MONDAY, Session.MORNING),
                    _slot(1, Weekday.MONDAY, Session.AFTERNOON),
                    _slot(1, Weekday.WEDNESDAY, Session.MORNING),
                ],
            )
        )
    assign_clinic_sites(instance, assignments)
    secondary_site = _secondary_site_id(instance)
    secondary_count = sum(1 for slot in slots if slot.site == secondary_site)
    assert secondary_count == _maximum_site_capacity(instance, secondary_site)


def test_pinned_maple_counts_toward_capacity() -> None:
    instance = sample_instance()
    secondary_site = _secondary_site_id(instance)
    cap = _maximum_site_capacity(instance, secondary_site)
    pinned_resident = instance.residents[0]
    flex_residents = instance.residents[1 : cap + 2]
    assignments = [
        Assignment(
            resident_id=pinned_resident.id,
            rotation_id="inpatient_peds_metro",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.FRIDAY,
                    session=Session.MORNING,
                    week=1,
                ),
                AssignedClinic(
                    weekday=Weekday.FRIDAY,
                    session=Session.AFTERNOON,
                    week=1,
                ),
            ],
        )
    ]
    for resident in flex_residents:
        assignments.append(
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    AssignedClinic(
                        weekday=Weekday.FRIDAY,
                        session=Session.MORNING,
                        week=1,
                    ),
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.MORNING,
                        week=1,
                    ),
                    AssignedClinic(
                        weekday=Weekday.MONDAY,
                        session=Session.AFTERNOON,
                        week=1,
                    ),
                    AssignedClinic(
                        weekday=Weekday.WEDNESDAY,
                        session=Session.MORNING,
                        week=1,
                    ),
                ],
            )
        )
    assign_clinic_sites(instance, assignments)
    friday_am = [
        slot
        for assignment in assignments
        for slot in assignment.clinic_slots
        if slot.weekday is Weekday.FRIDAY and slot.session is Session.MORNING
    ]
    secondary_count = sum(
        1 for slot in friday_am if slot.site == secondary_site
    )
    assert secondary_count <= cap
    assert assignments[0].clinic_slots[0].site == secondary_site


def test_maple_spreads_across_weeks() -> None:
    instance = sample_instance()
    residents = instance.residents[:4]
    assignments = []
    for week in range(1, 9):
        for resident in residents:
            assignments.append(
                Assignment(
                    resident_id=resident.id,
                    rotation_id="elective",
                    kind=RotationKind.STANDARD,
                    start_week=week,
                    end_week=week,
                    weeks=[week],
                    clinic_slots=[
                        _slot(week, Weekday.TUESDAY, Session.MORNING),
                        _slot(week, Weekday.MONDAY, Session.MORNING),
                    ],
                )
            )
    assign_clinic_sites(instance, assignments)
    by_week: dict[int, int] = defaultdict(int)
    for assignment in assignments:
        for slot in assignment.clinic_slots:
            if slot.site == _secondary_site_id(instance):
                by_week[slot.week] += 1
    counts = [by_week[week] for week in range(1, 9)]
    assert sum(counts) == 16
    assert max(counts) - min(counts) <= 2
    assert min(counts) >= 1


def test_maple_prefers_heavier_cedar_week_when_tied() -> None:
    instance = sample_instance()
    movers = instance.residents[:2]
    extras = instance.residents[2:10]
    assignments = []
    for resident in movers:
        for week in (2, 40):
            assignments.append(
                Assignment(
                    resident_id=resident.id,
                    rotation_id="elective",
                    kind=RotationKind.STANDARD,
                    start_week=week,
                    end_week=week,
                    weeks=[week],
                    clinic_slots=[
                        _slot(week, Weekday.TUESDAY, Session.MORNING),
                        _slot(week, Weekday.TUESDAY, Session.AFTERNOON),
                        _slot(week, Weekday.THURSDAY, Session.MORNING),
                        _slot(week, Weekday.MONDAY, Session.MORNING),
                        _slot(week, Weekday.MONDAY, Session.AFTERNOON),
                        _slot(week, Weekday.WEDNESDAY, Session.MORNING),
                    ],
                )
            )
    for resident in extras:
        assignments.append(
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=40,
                end_week=40,
                weeks=[40],
                clinic_slots=[
                    _slot(40, Weekday.MONDAY, Session.MORNING),
                    _slot(40, Weekday.MONDAY, Session.AFTERNOON),
                    _slot(40, Weekday.WEDNESDAY, Session.MORNING),
                ],
            )
        )
    assign_clinic_sites(instance, assignments)
    by_week: dict[int, int] = defaultdict(int)
    for assignment in assignments:
        if assignment.resident_id not in {resident.id for resident in movers}:
            continue
        for slot in assignment.clinic_slots:
            if slot.site == _secondary_site_id(instance):
                by_week[slot.week] += 1
    assert by_week[40] >= by_week[2]
    weekly = clinic_weekly_attendings(
        instance,
        assignments,
        instance.clinic_policy.primary_site_id,
    )
    assert weekly[40] >= weekly.get(2, 0)


def test_generalized_allocator_uses_weekend_clinic_staffing() -> None:
    instance = sample_instance()
    raw = instance.clinic_policy.model_dump(mode="json")
    raw["sites"].append(
        {
            "id": "weekend",
            "code": "WKND",
            "name": "Weekend Clinic",
            "color": "#28735C",
            "residents_per_attending": 2,
            "half_days": [
                {
                    "weekday": "saturday",
                    "session": "morning",
                    "attendings": 1,
                    "min_residents": 0,
                }
            ],
            "closure_days": [],
        }
    )
    raw["allocation_rules"] = [
        {
            "clinic_id": site_id,
            "min_fraction": 0,
            "target_fraction": 1 if site_id == "weekend" else 0,
            "max_fraction": 1,
        }
        for site_id in ("maple", "cedar", "weekend")
    ]
    policy = ClinicPolicy.model_validate(raw)
    instance = instance.model_copy(update={"clinic_policy": policy})
    assignments = [
        Assignment(
            resident_id=resident.id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[_slot(1, Weekday.SATURDAY, Session.MORNING)],
        )
        for resident in instance.residents[:2]
    ]

    result = assign_clinic_sites(instance, assignments)

    assert all(
        assignment.clinic_slots[0].site == "weekend"
        for assignment in assignments
    )
    assert result.assigned_by_clinic["weekend"] == 2
    assert policy.max_capacity("weekend", Weekday.SATURDAY, Session.MORNING) == 2


def test_allocator_applies_specific_date_capacity_override() -> None:
    instance = sample_instance()
    raw = instance.clinic_policy.model_dump(mode="json")
    maple = next(site for site in raw["sites"] if site["id"] == "maple")
    maple["capacity_overrides"] = [
        {
            "date": "2026-07-07",
            "session": "morning",
            "attendings": 0,
            "min_residents": 0,
        }
    ]
    raw["allocation_rules"] = [
        {
            "clinic_id": "maple",
            "min_fraction": 0,
            "target_fraction": 1,
            "max_fraction": 1,
        },
        {
            "clinic_id": "cedar",
            "min_fraction": 0,
            "target_fraction": 0,
            "max_fraction": 1,
        },
    ]
    policy = ClinicPolicy.model_validate(raw)
    instance = instance.model_copy(update={"clinic_policy": policy})
    assignment = Assignment(
        resident_id=instance.residents[0].id,
        rotation_id="elective",
        kind=RotationKind.STANDARD,
        start_week=2,
        end_week=2,
        weeks=[2],
        clinic_slots=[_slot(2, Weekday.TUESDAY, Session.MORNING)],
    )

    assign_clinic_sites(instance, [assignment])

    assert assignment.clinic_slots[0].site == "cedar"
    assert policy.max_capacity("maple", Weekday.TUESDAY, Session.MORNING) == 4
    assert policy.max_capacity_on(
        "maple",
        date(2026, 7, 7),
        Session.MORNING,
    ) == 0


def test_allocator_prefers_resident_override_over_pgy_override() -> None:
    instance = sample_instance()
    raw = instance.clinic_policy.model_dump(mode="json")
    raw["allocation_rules"].extend(
        [
            {"clinic_id": "maple", "pgy": 1, "target_fraction": 1},
            {"clinic_id": "cedar", "pgy": 1, "target_fraction": 0},
            {
                "clinic_id": "maple",
                "resident_id": "resident-001",
                "target_fraction": 0,
            },
            {
                "clinic_id": "cedar",
                "resident_id": "resident-001",
                "target_fraction": 1,
            },
        ]
    )
    policy = ClinicPolicy.model_validate(raw)
    instance = instance.model_copy(update={"clinic_policy": policy})
    assignments = []
    for resident in instance.residents[:2]:
        assignments.append(
            Assignment(
                resident_id=resident.id,
                rotation_id="elective",
                kind=RotationKind.STANDARD,
                start_week=1,
                end_week=1,
                weeks=[1],
                clinic_slots=[
                    _slot(1, Weekday.TUESDAY, Session.MORNING),
                    _slot(1, Weekday.TUESDAY, Session.AFTERNOON),
                    _slot(1, Weekday.THURSDAY, Session.MORNING),
                    _slot(1, Weekday.THURSDAY, Session.AFTERNOON),
                ],
            )
        )

    assign_clinic_sites(instance, assignments)

    assert {slot.site for slot in assignments[0].clinic_slots} == {"cedar"}
    assert {slot.site for slot in assignments[1].clinic_slots} == {"maple"}
