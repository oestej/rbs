from datetime import date, timedelta

from rbs.catalog import sample_instance
from rbs.models.enums import (
    RotationKind,
    Session,
    SolverEngineName,
    SolverStatus,
    Weekday,
)
from rbs.models.instance import SchedulerInput
from rbs.models.rotation import ClinicPolicy
from rbs.models.schedule import AssignedClinic, Assignment, Schedule, ScheduleMeta
from rbs.solver.validation import validate_schedule
from rbs.ui.clinic.board import (
    ACADEMIC_LABEL,
    ClinicOccupant,
    attending_load,
    calendar_occupants,
    clinic_closure_view,
    clinic_headcount,
    clinic_kind_slots,
    is_academic,
    occupancy,
    render_clinic_html,
    render_clinic_legend_html,
    weekly_attending_sessions,
)


def _with_closures(instance, closures: list[dict]):
    raw = instance.clinic_policy.model_dump(mode="json")
    raw["closure_days"] = closures
    return instance.model_copy(
        update={"clinic_policy": ClinicPolicy.model_validate(raw)}
    )


def _with_academic_override(
    instance: SchedulerInput,
    *,
    week: int,
    weekday: Weekday,
    session: Session,
) -> SchedulerInput:
    raw = instance.model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {
            "week": week,
            "weekday": weekday.value,
            "session": session.value,
        }
    ]
    return SchedulerInput.model_validate(raw)


def _schedule(*assignments: Assignment) -> Schedule:
    instance = sample_instance()
    return Schedule(
        meta=ScheduleMeta(
            academic_year=instance.academic_year,
            engine=SolverEngineName.STUB,
            status=SolverStatus.FEASIBLE,
        ),
        assignments=list(assignments),
    )


def _secondary_site_id(instance: SchedulerInput) -> str:
    return next(
        site_id
        for site_id in instance.clinic_policy.site_ids
        if site_id != instance.clinic_policy.primary_site_id
    )


def _clinic_assignment(
    resident_id: str,
    weeks: list[int],
    *,
    admin: tuple[Weekday, Session] | None = None,
) -> Assignment:
    slots = []
    if admin is not None:
        slots = [AssignedClinic(weekday=admin[0], session=admin[1], admin=True)]
    return Assignment(
        resident_id=resident_id,
        rotation_id="clinic",
        kind=RotationKind.CLINIC,
        start_week=weeks[0],
        end_week=weeks[-1],
        weeks=weeks,
        clinic_slots=slots,
    )


def _overlay_assignment(
    resident_id: str,
    weeks: list[int],
    weekday: Weekday,
    session: Session,
    rotation_id: str = "emergency_medicine",
) -> Assignment:
    return Assignment(
        resident_id=resident_id,
        rotation_id=rotation_id,
        kind=RotationKind.STANDARD,
        start_week=weeks[0],
        end_week=weeks[-1],
        weeks=weeks,
        clinic_slots=[AssignedClinic(weekday=weekday, session=session)],
    )


def test_wednesday_afternoon_is_academic() -> None:
    instance = sample_instance()
    policy = instance.clinic_policy
    assert is_academic(policy, Weekday.WEDNESDAY, Session.AFTERNOON)
    assert not is_academic(policy, Weekday.WEDNESDAY, Session.MORNING)
    assert (Weekday.WEDNESDAY, Session.AFTERNOON) not in clinic_kind_slots(instance)
    assert len(clinic_kind_slots(instance)) == 9


def test_week_override_moves_academic_and_opens_the_recurring_slot() -> None:
    instance = _with_academic_override(
        sample_instance(),
        week=1,
        weekday=Weekday.TUESDAY,
        session=Session.MORNING,
    )
    resident = instance.residents[0]
    schedule = _schedule(_clinic_assignment(resident.id, [1]))

    board = occupancy(instance, schedule)

    assert board[(1, Weekday.TUESDAY, Session.MORNING)] == []
    assert any(
        person.resident_id == resident.id
        for person in board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)]
    )
    assert instance.is_academic_half_day(
        1,
        Weekday.TUESDAY,
        Session.MORNING,
    )
    assert not instance.is_academic_half_day(
        1,
        Weekday.WEDNESDAY,
        Session.AFTERNOON,
    )

    invalid = _schedule(
        Assignment(
            resident_id=resident.id,
            rotation_id="elective",
            kind=RotationKind.ELECTIVE,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    week=1,
                )
            ],
        )
    )
    errors = validate_schedule(instance, invalid).errors
    assert any("week 1: clinic overlaps academic half day" in error for error in errors)


def test_empty_schedule_has_no_occupants_but_academic_cells() -> None:
    instance = sample_instance()
    board = occupancy(instance, schedule=None)
    assert board[(1, Weekday.MONDAY, Session.MORNING)] == []
    assert board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    markup = render_clinic_html(instance, schedule=None)
    assert ACADEMIC_LABEL in markup
    assert markup.count('class="rbs-clinic-session academic"') == instance.calendar.weeks
    assert '<time datetime="2026-06-29">Jun 29</time>' in markup
    assert '<time datetime="2026-07-06">Jul 6</time>' in markup
    assert "Monday" in markup and "Friday" in markup
    assert '>AM</span>' in markup
    assert '>PM</span>' in markup
    assert "rbs-clinic-summary" not in markup
    assert "Peak" not in markup


def test_schedule_validation_uses_specific_date_capacity_override() -> None:
    instance = sample_instance()
    raw = instance.clinic_policy.model_dump(mode="json")
    maple = next(site for site in raw["sites"] if site["id"] == "maple")
    maple["capacity_overrides"] = [
        {
            "date": "2026-06-30",
            "session": "morning",
            "attendings": 0,
            "min_residents": 0,
        }
    ]
    instance = instance.model_copy(
        update={"clinic_policy": ClinicPolicy.model_validate(raw)}
    )
    schedule = _schedule(
        Assignment(
            resident_id=instance.residents[0].id,
            rotation_id="elective",
            kind=RotationKind.ELECTIVE,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site="maple",
                    week=1,
                )
            ],
        )
    )

    errors = validate_schedule(instance, schedule).errors

    assert any(
        "Maple clinic has no attending coverage: week 1 tuesday morning" in error
        for error in errors
    )


def test_christmas_is_a_full_closure_for_both_configured_clinics() -> None:
    instance = sample_instance()
    christmas = date(2026, 12, 25)
    view = clinic_closure_view(instance.clinic_policy, christmas)

    assert view.all_selected_sites_closed
    assert set(view.closed_site_names) == {"Maple", "Cedar"}
    assert view.label() == "Christmas · Closed"

    markup = render_clinic_html(instance, schedule=None)
    assert 'class="rbs-clinic-day closure-full"' in markup
    assert '<time datetime="2026-12-25">Dec 25</time>' in markup
    assert "Christmas · Closed" in markup
    assert markup.count('class="rbs-clinic-session closure"') == 2


def test_partial_closure_keeps_open_sites_available_and_marks_the_calendar() -> None:
    instance = _with_closures(
        sample_instance(),
        [
            {
                "date": "2026-07-07",
                "name": "Site maintenance",
                "sites": ["maple"],
            }
        ],
    )
    maple_resident = instance.residents[0]
    cedar_resident = instance.residents[1]
    schedule = _schedule(
        Assignment(
            resident_id=maple_resident.id,
            rotation_id="elective",
            kind=RotationKind.ELECTIVE,
            elective=True,
            start_week=2,
            end_week=2,
            weeks=[2],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site="maple",
                    week=2,
                )
            ],
        ),
        Assignment(
            resident_id=cedar_resident.id,
            rotation_id="elective",
            kind=RotationKind.ELECTIVE,
            elective=True,
            start_week=2,
            end_week=2,
            weeks=[2],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site="cedar",
                    week=2,
                )
            ],
        ),
    )

    people = occupancy(instance, schedule)[(2, Weekday.TUESDAY, Session.MORNING)]
    assert [person.name for person in people] == [cedar_resident.name]

    all_sites = render_clinic_html(instance, schedule)
    assert 'class="rbs-clinic-day closure-partial"' in all_sites
    assert "Site maintenance · Maple closed" in all_sites
    assert cedar_resident.name in all_sites
    assert maple_resident.name not in all_sites

    maple_only = render_clinic_html(instance, schedule, site="maple")
    assert 'class="rbs-clinic-day closure-full"' in maple_only
    assert "Site maintenance · Closed" in maple_only

    cedar_only = render_clinic_html(instance, schedule, site="cedar")
    assert "closure-full" not in cedar_only
    assert "closure-partial" not in cedar_only
    assert cedar_resident.name in cedar_only

    validation = validate_schedule(instance, schedule)
    assert any(
        "Maple is closed on July 7, 2026 (Site maintenance)" in error
        for error in validation.errors
    )


def test_clinic_board_can_hide_past_week_rows() -> None:
    markup = render_clinic_html(
        sample_instance(),
        schedule=None,
        show_past_weeks=False,
        today=date(2026, 8, 22),
    )

    assert '<time datetime="2026-08-17">Aug 17</time>' in markup
    assert 'datetime="2026-08-10"' not in markup
    assert 'datetime="2026-06-29"' not in markup


def test_clinic_kind_fills_all_half_days_except_academic() -> None:
    instance = sample_instance()
    avery = instance.residents[0]
    schedule = _schedule(_clinic_assignment(avery.id, [1, 2]))
    board = occupancy(instance, schedule)
    names = {person.name for person in board[(1, Weekday.MONDAY, Session.MORNING)]}
    assert avery.name in names
    friday_pm = board[(1, Weekday.FRIDAY, Session.AFTERNOON)]
    wed_am = board[(2, Weekday.WEDNESDAY, Session.MORNING)]
    assert any(person.name == avery.name for person in friday_pm)
    assert any(person.name == avery.name for person in wed_am)
    assert board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    assert board[(3, Weekday.MONDAY, Session.MORNING)] == []
    markup = render_clinic_html(instance, schedule)
    assert avery.name in markup
    assert f"PGY{avery.pgy} {avery.name}" in markup
    assert f'>PGY{avery.pgy}</span>' in markup
    assert ACADEMIC_LABEL in markup


def test_clinic_kind_admin_half_day_is_marked() -> None:
    instance = sample_instance()
    avery = instance.residents[0]
    schedule = _schedule(_clinic_assignment(avery.id, [1], admin=(Weekday.MONDAY, Session.MORNING)))
    board = occupancy(instance, schedule)
    monday_am = board[(1, Weekday.MONDAY, Session.MORNING)]
    assert len(monday_am) == 1
    assert monday_am[0].admin
    assert monday_am[0].label() == f"PGY{avery.pgy} {avery.name} · Admin"
    tuesday_am = board[(1, Weekday.TUESDAY, Session.MORNING)]
    assert len(tuesday_am) == 1
    assert not tuesday_am[0].admin
    assert board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    markup = render_clinic_html(instance, schedule)
    given_name, last_name = monday_am[0].name.rsplit(" ", 1)
    assert (
        f'<span class="rbs-clinic-training-level">PGY{monday_am[0].pgy}</span> '
        f"{given_name} "
        f'<strong class="rbs-clinic-last-name">{last_name}</strong></div>'
    ) in markup
    assert f">{monday_am[0].label()}</div>" not in markup
    assert 'class="rbs-clinic-person admin"' in markup
    assert "rbs-clinic-swatch admin" in markup


def test_access_and_cedar_shown_on_board() -> None:
    instance = sample_instance()
    intern = instance.residents[0]
    senior = next(resident for resident in instance.residents if resident.pgy == 2)
    schedule = _schedule(
        Assignment(
            resident_id=intern.id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site=_secondary_site_id(instance),
                    week=1,
                )
            ],
        ),
        Assignment(
            resident_id=senior.id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site=instance.clinic_policy.primary_site_id,
                    week=1,
                )
            ],
        ),
    )
    board = occupancy(instance, schedule)
    people = board[(1, Weekday.TUESDAY, Session.MORNING)]
    sites = {person.site for person in people}
    assert sites == set(instance.clinic_policy.site_ids)
    markup = render_clinic_html(instance, schedule)
    for person in people:
        given_name, last_name = person.name.rsplit(" ", 1)
        assert (
            f'<span class="rbs-clinic-training-level">PGY{person.pgy}</span> '
            f"{given_name} "
            f'<strong class="rbs-clinic-last-name">{last_name}</strong></div>'
        ) in markup
        assert f">{person.label()}</div>" not in markup
    assert ">1 Maple</span>" in markup
    assert ">1 Cedar</span>" in markup
    assert " ATT " not in markup
    assert 'class="rbs-clinic-session-attending"' in markup
    assert markup.count('class="rbs-clinic-person site"') == 2
    assert "--rbs-clinic-site-color:#6D6BC2" in markup
    assert "--rbs-clinic-site-color:#174A7E" in markup
    assert markup.count("rbs-clinic-swatch site") == 2


def test_clinic_board_filters_residents_and_attendings_to_one_site() -> None:
    instance = sample_instance()
    maple_resident = instance.residents[0]
    cedar_resident = next(resident for resident in instance.residents if resident.pgy == 2)
    schedule = _schedule(
        Assignment(
            resident_id=maple_resident.id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site=_secondary_site_id(instance),
                    week=1,
                )
            ],
        ),
        Assignment(
            resident_id=cedar_resident.id,
            rotation_id="elective",
            kind=RotationKind.STANDARD,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(
                    weekday=Weekday.TUESDAY,
                    session=Session.MORNING,
                    site=instance.clinic_policy.primary_site_id,
                    week=1,
                )
            ],
        ),
    )

    markup = render_clinic_html(
        instance,
        schedule,
        site=_secondary_site_id(instance),
        show_legend=False,
    )

    assert maple_resident.name in markup
    assert cedar_resident.name not in markup
    assert ">1 Maple</span>" in markup
    assert "Cedar</span>" not in markup
    assert " ATT " not in markup
    assert "rbs-clinic-swatch" not in markup


def test_clinic_legend_includes_every_supported_site_and_admin() -> None:
    instance = sample_instance()
    legend = render_clinic_legend_html(instance.clinic_policy)

    assert legend.count('class="rbs-clinic-swatch site"') == 2
    assert "Maple" in legend
    assert "Cedar" in legend
    assert "Harbor" not in legend
    assert "--rbs-clinic-site-tint:#F0F0F9" in legend
    assert "--rbs-clinic-site-tint:#E8EDF2" in legend
    assert 'class="rbs-clinic-swatch admin"' in legend


def test_pgy_year_shown_for_interns_and_seniors() -> None:
    instance = sample_instance()
    intern = next(resident for resident in instance.residents if resident.pgy == 1)
    senior = next(resident for resident in instance.residents if resident.pgy == 2)
    schedule = _schedule(
        _clinic_assignment(intern.id, [1], admin=(Weekday.FRIDAY, Session.AFTERNOON)),
        _overlay_assignment(senior.id, [1], Weekday.MONDAY, Session.AFTERNOON),
    )
    markup = render_clinic_html(instance, schedule)
    assert f"PGY1 {intern.name}" in markup
    assert f"PGY2 {senior.name}" in markup
    assert ">PGY1</span>" in markup
    assert ">PGY2</span>" in markup


def test_overlay_slot_appears_only_in_that_half_day() -> None:
    instance = sample_instance()
    jordan = next(resident for resident in instance.residents if resident.id == "resident-002")
    schedule = _schedule(
        _overlay_assignment(jordan.id, [1], Weekday.MONDAY, Session.AFTERNOON),
    )
    board = occupancy(instance, schedule)
    assert [person.name for person in board[(1, Weekday.MONDAY, Session.AFTERNOON)]] == [
        jordan.name
    ]
    assert board[(1, Weekday.MONDAY, Session.MORNING)] == []
    assert board[(1, Weekday.TUESDAY, Session.AFTERNOON)] == []
    assert board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)] == []


def test_away_rotation_suppresses_a_resident_clinic_half_day() -> None:
    instance = sample_instance()
    resident = instance.residents[0]
    assignment = Assignment(
        resident_id=resident.id,
        rotation_id="peds_community",
        kind=RotationKind.STANDARD,
        start_week=1,
        end_week=1,
        weeks=[1],
        clinic_slots=[
            AssignedClinic(
                weekday=Weekday.TUESDAY,
                session=Session.MORNING,
                site=instance.clinic_policy.primary_site_id,
                week=1,
            )
        ],
    )
    schedule = _schedule(assignment)
    assert [
        person.name
        for person in occupancy(instance, schedule)[
            (1, Weekday.TUESDAY, Session.MORNING)
        ]
    ] == [resident.name]

    away = instance.rotation("peds_community").model_copy(update={"away": True})
    away_instance = instance.model_copy(
        update={
            "rotations": [
                away if rotation.id == away.id else rotation
                for rotation in instance.rotations
            ]
        }
    )
    assert occupancy(away_instance, schedule)[
        (1, Weekday.TUESDAY, Session.MORNING)
    ] == []


def test_vacation_week_omits_resident_from_clinic() -> None:
    instance = sample_instance()
    avery = next(resident for resident in instance.residents if resident.id == "resident-001")
    assert 12 in avery.vacation_weeks
    assert 11 not in avery.vacation_weeks
    schedule = _schedule(_clinic_assignment(avery.id, [11, 12]))
    board = occupancy(instance, schedule)
    assert board[(12, Weekday.MONDAY, Session.MORNING)] == []
    assert any(person.name == avery.name for person in board[(11, Weekday.MONDAY, Session.MORNING)])
    assert board[(12, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    assert board[(11, Weekday.WEDNESDAY, Session.AFTERNOON)] == []


def test_individual_day_off_omits_only_that_day_from_clinic() -> None:
    instance = sample_instance()
    avery = next(resident for resident in instance.residents if resident.id == "resident-001")
    tuesday_off = instance.calendar.first_week_start + timedelta(weeks=10, days=1)
    updated_avery = avery.model_copy(update={"days_off": [tuesday_off]})
    residents = [
        updated_avery if resident.id == avery.id else resident
        for resident in instance.residents
    ]
    instance = instance.model_copy(update={"residents": residents})
    schedule = _schedule(_clinic_assignment(avery.id, [11]))

    board = occupancy(instance, schedule)

    assert board[(11, Weekday.TUESDAY, Session.MORNING)] == []
    assert board[(11, Weekday.TUESDAY, Session.AFTERNOON)] == []
    assert any(person.name == avery.name for person in board[(11, Weekday.MONDAY, Session.MORNING)])
    assert any(
        person.name == avery.name
        for person in board[(11, Weekday.WEDNESDAY, Session.MORNING)]
    )


def test_attendings_count_clinic_residents_not_admin() -> None:
    instance = sample_instance()
    intern = next(resident for resident in instance.residents if resident.pgy == 1)
    others = [resident for resident in instance.residents if resident.id != intern.id][:4]
    schedule = _schedule(
        _clinic_assignment(intern.id, [1], admin=(Weekday.MONDAY, Session.MORNING)),
        *[
            _overlay_assignment(resident.id, [1], Weekday.MONDAY, Session.AFTERNOON)
            for resident in others
        ],
    )
    board = occupancy(instance, schedule)
    monday_am = board[(1, Weekday.MONDAY, Session.MORNING)]
    monday_pm = board[(1, Weekday.MONDAY, Session.AFTERNOON)]
    policy = instance.clinic_policy
    assert clinic_headcount(monday_am) == 0
    assert policy.attendings_needed(clinic_headcount(monday_am)) == 0
    assert clinic_headcount(monday_pm) == 5
    assert policy.attendings_needed(clinic_headcount(monday_pm)) == 2
    peak, total = attending_load(instance, schedule)
    assert peak == 2
    assert total >= 2
    markup = render_clinic_html(instance, schedule)
    assert ">2 Cedar</span>" in markup
    assert "Peak" not in markup
    assert "attending-sessions" not in markup
    assert "att/week" not in markup
    weekly = weekly_attending_sessions(
        instance,
        schedule,
        site=instance.clinic_policy.primary_site_id,
    )
    assert weekly[1] >= 2


def test_calendar_occupants_sort_by_site_then_pgy_then_last_name() -> None:
    instance = sample_instance()
    policy = instance.clinic_policy
    secondary = _secondary_site_id(instance)
    primary = policy.primary_site_id
    people = [
        ClinicOccupant("primary", "Zed Yellow", 1, site=primary),
        ClinicOccupant("secondary-z", "Amy Zebra", 2, site=secondary),
        ClinicOccupant("secondary-a", "Bea Adams", 2, site=secondary),
        ClinicOccupant("secondary-pgy1", "Cal Baker", 1, site=secondary),
    ]

    ordered = calendar_occupants(people, policy)

    assert [person.resident_id for person in ordered] == [
        "secondary-pgy1",
        "secondary-a",
        "secondary-z",
        "primary",
    ]


def test_academic_half_day_blocked_even_if_slot_listed() -> None:
    instance = sample_instance()
    avery = instance.residents[0]
    schedule = _schedule(
        Assignment(
            resident_id=avery.id,
            rotation_id="fmed",
            kind=RotationKind.FMED,
            start_week=1,
            end_week=1,
            weeks=[1],
            clinic_slots=[
                AssignedClinic(weekday=Weekday.WEDNESDAY, session=Session.AFTERNOON),
            ],
        )
    )
    board = occupancy(instance, schedule)
    assert board[(1, Weekday.WEDNESDAY, Session.AFTERNOON)] == []
    markup = render_clinic_html(instance, schedule)
    assert markup.count('class="rbs-clinic-session academic"') == instance.calendar.weeks
    assert avery.name not in markup
