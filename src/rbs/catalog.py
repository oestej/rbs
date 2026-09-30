"""Load configurable catalog data and build a demonstration workspace.

Program-specific rotations, clinic sites, capacities, allocation targets, and
placement rules live in the bundled JSON catalog rather than Python code.
``rbs.models.catalog`` holds the ``ConstraintCatalog`` model itself; this
module holds the bundled data and the sample/blank instance builders.
"""

from datetime import date, timedelta
from pathlib import Path

from rbs.academic_year import (
    academic_year_for_date,
    first_week_start_for_academic_year,
    rebase_academic_year,
)
from rbs.models.attending import (
    Attending,
    AttendingSchedule,
    AttendingVacation,
    AttendingWeeklyShiftTarget,
    AttendingWeeklyTargetMode,
    AttendingWeeklyWorkSchedule,
    AttendingWorkHalfDay,
    AttendingWorkType,
)
from rbs.models.catalog import ConstraintCatalog
from rbs.models.clinic import (
    ALL_CLINIC_SITES,
    ClinicAllocationRule,
    ClinicPolicy,
    ClinicRule,
    ClinicSiteConfig,
    ClinicSlot,
)
from rbs.models.color_scheme import DEFAULT_COLOR_SCHEME
from rbs.models.curriculum import PGYCurriculum
from rbs.models.elective import ElectiveConfiguration
from rbs.models.enums import WEEKDAYS_MF, RotationKind, Session, Weekday
from rbs.models.instance import (
    AcademicHalfDayOverride,
    Calendar,
    SchedulerInput,
    SolverConfig,
)
from rbs.models.locks import LockedPlacement
from rbs.models.resident import ElectivePreferenceRequest, Resident
from rbs.models.rotation import (
    PGYRotationRule,
    Rotation,
    RotationBlockConfig,
    VacationRule,
)
from rbs.models.special import SpecialRotation, SpecialRotationKind


def monday_of_week_containing(day: date) -> date:
    return day - timedelta(days=day.weekday())


def bundled_catalog_path() -> Path:
    packaged = Path(__file__).with_name("data") / "catalog.json"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "data" / "catalog.json"


def bundled_catalog() -> ConstraintCatalog:
    """Load the checked-in catalog used by the demo and new workspaces."""
    path = bundled_catalog_path()
    if not path.exists():  # pragma: no cover - invalid package guard
        raise FileNotFoundError(f"bundled constraint catalog not found: {path}")
    return ConstraintCatalog.model_validate_json(path.read_text(encoding="utf-8"))


def bootstrap_catalog() -> ConstraintCatalog:
    """Return a detached copy of the catalog used to initialize storage."""
    return ConstraintCatalog.model_validate(bundled_catalog().model_dump(mode="json"))


def default_rotations() -> list[Rotation]:
    """Compatibility accessor for the JSON-backed demonstration catalog."""
    return [rotation.model_copy(deep=True) for rotation in bundled_catalog().rotations]


def default_requirements() -> list[PGYCurriculum]:
    """Compatibility accessor for the JSON-backed demonstration curriculum."""
    return [requirement.model_copy(deep=True) for requirement in bundled_catalog().requirements]


def default_clinic_policy() -> ClinicPolicy:
    """Compatibility accessor for the JSON-backed demonstration clinic policy."""
    return bundled_catalog().clinic_policy.model_copy(deep=True)


def catalog_dict() -> dict:
    """The JSON-backed catalog, suitable for merging into an instance."""
    return bundled_catalog().model_dump(mode="json")


def sample_residents() -> list[Resident]:
    """Create placeholder residents and time-off requests for UI demonstration."""
    pgy1 = [
        Resident(id="resident-001", name="Avery Chen", pgy=1, vacation_weeks=[12, 13, 28, 41]),
        Resident(id="resident-002", name="Jordan Patel", pgy=1, vacation_weeks=[8, 24, 40, 51]),
        Resident(id="resident-003", name="Sam Rivera", pgy=1, vacation_weeks=[4, 20, 36, 47]),
        Resident(id="resident-004", name="Riley Nguyen", pgy=1, vacation_weeks=[7, 16, 32, 48]),
        Resident(id="resident-005", name="Quinn Brooks", pgy=1, vacation_weeks=[6, 22, 33, 44]),
        Resident(id="resident-006", name="Morgan Ellis", pgy=1, vacation_weeks=[9, 14, 30, 46]),
        Resident(id="resident-007", name="Casey Okonkwo", pgy=1, vacation_weeks=[10, 19, 26, 42]),
        Resident(id="resident-008", name="Harper Diaz", pgy=1, vacation_weeks=[3, 18, 34, 50]),
    ]
    pgy2 = [
        Resident(id="resident-009", name="Taylor Kim", pgy=2, vacation_weeks=[5, 21, 37, 48]),
        Resident(id="resident-010", name="Jamie Alvarez", pgy=2, vacation_weeks=[9, 25, 41, 52]),
        Resident(id="resident-011", name="Drew Hassan", pgy=2, vacation_weeks=[11, 16, 27, 43]),
        Resident(id="resident-012", name="Cameron Walsh", pgy=2, vacation_weeks=[7, 23, 39, 50]),
        Resident(id="resident-013", name="Reese Nakamura", pgy=2, vacation_weeks=[4, 15, 31, 47]),
        Resident(id="resident-014", name="Skyler Bennett", pgy=2, vacation_weeks=[3, 19, 35, 44]),
        Resident(id="resident-015", name="Alex Rahman", pgy=2, vacation_weeks=[8, 17, 33, 49]),
        Resident(id="resident-016", name="Peyton Ortiz", pgy=2, vacation_weeks=[2, 14, 29, 45]),
    ]
    pgy3 = [
        Resident(id="resident-017", name="Robin Ford", pgy=3, vacation_weeks=[6, 18, 34, 46]),
        Resident(id="resident-018", name="Devon Park", pgy=3, vacation_weeks=[10, 22, 38, 50]),
        Resident(id="resident-019", name="Emerson Cole", pgy=3, vacation_weeks=[4, 16, 30, 44]),
        Resident(id="resident-020", name="Finley Adams", pgy=3, vacation_weeks=[8, 24, 36, 49]),
        Resident(id="resident-021", name="Hayden Ross", pgy=3, vacation_weeks=[5, 19, 33, 48]),
        Resident(id="resident-022", name="Charlie Singh", pgy=3, vacation_weeks=[7, 21, 37, 51]),
        Resident(id="resident-023", name="Rowan Baker", pgy=3, vacation_weeks=[9, 23, 39, 52]),
        Resident(id="resident-024", name="Sydney Cho", pgy=3, vacation_weeks=[11, 25, 35, 45]),
    ]
    return pgy1 + pgy2 + pgy3


def _night_float_request() -> ElectivePreferenceRequest:
    """One reusable 2-week Night Float elective request for the demo cohort."""
    return ElectivePreferenceRequest(rotation_id="night_float", duration_weeks=2)


def _fmed_request() -> ElectivePreferenceRequest:
    """One reusable 2-week FMED elective request for the demo cohort."""
    return ElectivePreferenceRequest(rotation_id="fmed", duration_weeks=2)


def _geriatrics_request() -> ElectivePreferenceRequest:
    """One reusable 2-week Geriatrics elective request for the demo cohort."""
    return ElectivePreferenceRequest(rotation_id="geriatrics", duration_weeks=2)


def _palliative_care_request() -> ElectivePreferenceRequest:
    """One reusable 2-week Palliative Care elective request for the demo cohort."""
    return ElectivePreferenceRequest(rotation_id="palliative_care", duration_weeks=2)


def sample_elective_preferences(
    resident_id: str,
    pgy: int,
) -> list[ElectivePreferenceRequest]:
    """Stack-ranked elective requests matching the demo elective policy.

    Requests cycle across the configured non-clinic options so the demo
    cohort exercises each elective service.
    """
    slot = int(resident_id.rsplit("-", 1)[-1])
    if pgy == 1:
        return [
            [_night_float_request(), _geriatrics_request(), _palliative_care_request()][
                (slot - 1) % 3
            ]
        ]
    if pgy == 2:
        if slot % 2 == 0:
            return [_fmed_request(), _fmed_request(), _night_float_request()]
        return [_fmed_request(), _geriatrics_request(), _palliative_care_request()]
    return []


def sample_days_off(resident_id: str, first_week_start: date) -> list[date]:
    """Individual days off illustrating mid-week requests in the demo cohort."""
    offsets = {
        "resident-002": [9 * 7 + 2],
        "resident-005": [19 * 7 + 3],
        "resident-010": [29 * 7 + 1],
        "resident-017": [14 * 7 + 4],
    }
    return [first_week_start + timedelta(days=offset) for offset in offsets.get(resident_id, [])]


SAMPLE_PRECEPTING_HALF_DAYS_PER_WEEK = 3


def _sample_targets(
    *targets: tuple[AttendingWorkType, int, int, AttendingWeeklyTargetMode],
) -> list[AttendingWeeklyShiftTarget]:
    """Every sample attending precepts exactly three half-days a week."""
    precepting = AttendingWeeklyShiftTarget(
        work_type=AttendingWorkType.PRECEPTING_CLINIC,
        minimum_shifts_per_week=SAMPLE_PRECEPTING_HALF_DAYS_PER_WEEK,
        maximum_shifts_per_week=SAMPLE_PRECEPTING_HALF_DAYS_PER_WEEK,
        mode=AttendingWeeklyTargetMode.FIXED,
    )
    return [
        precepting,
        *(
            AttendingWeeklyShiftTarget(
                work_type=work_type,
                minimum_shifts_per_week=minimum,
                maximum_shifts_per_week=maximum,
                mode=mode,
            )
            for work_type, minimum, maximum, mode in targets
        ),
    ]


def _sample_half_days(
    *half_days: tuple[Weekday, Session, AttendingWorkType, str | None],
) -> list[AttendingWorkHalfDay]:
    return [
        AttendingWorkHalfDay(
            weekday=weekday,
            session=session,
            work_type=work_type,
            clinic_id=clinic_id,
        )
        for weekday, session, work_type, clinic_id in half_days
    ]


def sample_attendings(first_week_start: date) -> list[Attending]:
    """Create an illustrative faculty whose work the solve schedules.

    Each attending precepts three half-days a week. Their preferred precepting
    half-days fall when the sample clinics run: Maple on Tuesday and Thursday
    and Friday mornings with room for one attending, Cedar on weekday half-days
    with room for four. Schedule dates and day-level vacations vary so the
    solve has partial weeks to work around.
    """
    last_day = first_week_start + timedelta(days=52 * 7 - 1)
    fixed = AttendingWeeklyTargetMode.FIXED
    flexible = AttendingWeeklyTargetMode.FLEXIBLE
    clinic = AttendingWorkType.ATTENDING_CLINIC
    precepting = AttendingWorkType.PRECEPTING_CLINIC
    inpatient = AttendingWorkType.INPATIENT_SERVICE
    admin = AttendingWorkType.ADMIN_TIME
    monday, tuesday, wednesday, thursday, friday = WEEKDAYS_MF
    morning, afternoon = Session.MORNING, Session.AFTERNOON
    return [
        Attending(
            id="attending-001",
            name="Maya Singh",
            weekly_shift_targets=_sample_targets(
                (clinic, 4, 5, flexible),
                (admin, 1, 2, flexible),
            ),
            minimum_attending_clinic_days_per_week=2,
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, morning, clinic, None),
                (monday, afternoon, clinic, None),
                (tuesday, morning, precepting, "maple"),
                (thursday, morning, clinic, None),
                (thursday, afternoon, precepting, "maple"),
                (friday, morning, precepting, "cedar"),
            ),
            vacation_ranges=[
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=44),
                    end_date=first_week_start + timedelta(days=50),
                ),
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=178),
                    end_date=first_week_start + timedelta(days=183),
                ),
            ],
        ),
        Attending(
            id="attending-002",
            name="Noah Williams",
            schedule_start_date=first_week_start + timedelta(days=33),
            weekly_shift_targets=_sample_targets(
                (inpatient, 2, 2, fixed),
                (clinic, 3, 4, flexible),
                (admin, 1, 2, flexible),
            ),
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, morning, precepting, "cedar"),
                (tuesday, morning, inpatient, None),
                (tuesday, afternoon, inpatient, None),
                (wednesday, morning, precepting, "cedar"),
                (friday, afternoon, precepting, "cedar"),
            ),
            vacation_ranges=[
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=101),
                    end_date=first_week_start + timedelta(days=101),
                )
            ],
        ),
        Attending(
            id="attending-003",
            name="Elena Garcia",
            schedule_end_date=last_day - timedelta(days=28),
            weekly_shift_targets=_sample_targets(
                (clinic, 4, 6, flexible),
                (admin, 1, 2, flexible),
            ),
            minimum_attending_clinic_days_per_week=3,
            preferred_weekly_schedule_half_days=_sample_half_days(
                (tuesday, afternoon, precepting, "cedar"),
                (thursday, morning, precepting, "cedar"),
                (friday, morning, precepting, "maple"),
            ),
            vacation_ranges=[
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=250),
                    end_date=first_week_start + timedelta(days=259),
                )
            ],
        ),
        Attending(
            id="attending-004",
            name="Theo Brooks",
            # Part time: three precepting half-days plus the academic
            # half-day's automatic Admin Time.
            half_days_per_week=4,
            schedule_start_date=first_week_start + timedelta(days=61),
            schedule_end_date=last_day - timedelta(days=42),
            weekly_shift_targets=_sample_targets(),
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, afternoon, precepting, "cedar"),
                (tuesday, morning, precepting, "cedar"),
                (thursday, afternoon, precepting, "cedar"),
            ),
        ),
        Attending(
            id="attending-005",
            name="Priya Patel",
            weekly_shift_targets=_sample_targets(
                (clinic, 4, 6, flexible),
                (admin, 1, 2, flexible),
            ),
            minimum_attending_clinic_days_per_week=2,
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, afternoon, precepting, "cedar"),
                (tuesday, afternoon, precepting, "maple"),
                (thursday, morning, precepting, "maple"),
            ),
            vacation_ranges=[
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=175),
                    end_date=first_week_start + timedelta(days=181),
                )
            ],
        ),
        Attending(
            id="attending-006",
            name="Samuel Okafor",
            half_days_per_week=8,
            weekly_shift_targets=_sample_targets(
                (inpatient, 2, 2, fixed),
                (clinic, 1, 2, flexible),
            ),
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, morning, precepting, "cedar"),
                (wednesday, morning, precepting, "cedar"),
                (thursday, afternoon, precepting, "cedar"),
            ),
        ),
        Attending(
            id="attending-007",
            name="Hannah Kim",
            # Joins the faculty partway through the academic year.
            schedule_start_date=first_week_start + timedelta(days=140),
            weekly_shift_targets=_sample_targets((clinic, 5, 6, flexible)),
            minimum_attending_clinic_days_per_week=3,
            preferred_weekly_schedule_half_days=_sample_half_days(
                (tuesday, morning, precepting, "cedar"),
                (thursday, morning, precepting, "cedar"),
                (friday, afternoon, precepting, "cedar"),
            ),
        ),
        Attending(
            id="attending-008",
            name="Daniel Reyes",
            half_days_per_week=6,
            weekly_shift_targets=_sample_targets(
                (clinic, 1, 2, flexible),
                (admin, 1, 2, flexible),
            ),
            preferred_weekly_schedule_half_days=_sample_half_days(
                (monday, morning, precepting, "cedar"),
                (tuesday, afternoon, precepting, "cedar"),
                (friday, morning, precepting, "cedar"),
            ),
            vacation_ranges=[
                AttendingVacation(
                    start_date=first_week_start + timedelta(days=301),
                    end_date=first_week_start + timedelta(days=305),
                )
            ],
        ),
    ]


def sample_attending_schedules() -> list[AttendingSchedule]:
    """Create illustrative accepted work without mixing it into roster inputs."""
    return [
        AttendingSchedule(
            attending_id="attending-004",
            weeks=[
                AttendingWeeklyWorkSchedule(
                    week=11,
                    # Two week-specific meetings, the automatically reserved
                    # academic Admin Time, and the usual three precepting
                    # half-days, which the solve places around the meetings.
                    half_days_override=6,
                    half_days=[
                        AttendingWorkHalfDay(
                            weekday=Weekday.MONDAY,
                            session=Session.MORNING,
                            description="Credentialing committee",
                        ),
                        AttendingWorkHalfDay(
                            weekday=Weekday.THURSDAY,
                            session=Session.AFTERNOON,
                            description="Community board meeting",
                        ),
                    ],
                ),
            ],
        ),
    ]


def sample_special_rotations(first_week_start: date) -> list[SpecialRotation]:
    """One dated conference illustrating workspace events in the demo cohort."""
    start = first_week_start + timedelta(days=120)
    return [
        SpecialRotation(
            id="demo-educators-summit",
            name="Educators Summit",
            kind=SpecialRotationKind.CONFERENCE,
            start_date=start,
            end_date=start + timedelta(days=2),
            resident_ids=["resident-017", "resident-018"],
        )
    ]


def sample_academic_overrides() -> list[AcademicHalfDayOverride]:
    """One moved academic half-day illustrating overrides in the demo cohort."""
    return [
        AcademicHalfDayOverride(week=20, weekday=Weekday.MONDAY, session=Session.MORNING)
    ]


SAMPLE_ACADEMIC_YEAR = "2026-2027"


def _default_fmed_rotation(
    *,
    training_level_ids: tuple[int, ...],
    academic: ClinicSlot,
    color: str,
) -> Rotation:
    """Seed the dedicated inpatient service that cannot be created from Mandatory.

    FMED/Inpatient uses a purpose-built editor and cannot be added as a
    Mandatory rotation, so every new workspace needs this rotation present.
    """
    return Rotation(
        id="fmed",
        code="FMED",
        name="Inpatient",
        color=color,
        kind=RotationKind.FMED,
        pgy_rules=[
            PGYRotationRule(
                pgy=pgy,
                block_configs=[RotationBlockConfig(duration_weeks=4)],
            )
            for pgy in training_level_ids
        ],
        clinic=ClinicRule(
            half_days_per_week=1,
            slots=[
                ClinicSlot(
                    weekday=weekday,
                    session=Session.AFTERNOON,
                    sites=[ALL_CLINIC_SITES],
                )
                for weekday in WEEKDAYS_MF
                if weekday is not academic.weekday
            ],
            max_concurrent=1,
        ),
        max_consecutive_weeks=4,
    )


def _default_clinic_rotation(
    *,
    training_level_ids: tuple[int, ...],
    color: str,
) -> Rotation:
    """Seed the dedicated Clinic block that cannot be created from Mandatory.

    Clinic blocks use a purpose-built editor and cannot be added as a
    Mandatory rotation, so every new workspace needs this rotation present.
    """
    return Rotation(
        id="clinic",
        code="CLINIC",
        name="Clinic",
        color=color,
        kind=RotationKind.CLINIC,
        pgy_rules=[
            PGYRotationRule(
                pgy=pgy,
                block_configs=[
                    RotationBlockConfig(
                        duration_weeks=2,
                        vacation=VacationRule(allowed=True),
                    )
                ],
            )
            for pgy in training_level_ids
        ],
        clinic=ClinicRule(
            half_days_per_week=1,
            slots=[
                ClinicSlot(
                    weekday=weekday,
                    session=session,
                    sites=[ALL_CLINIC_SITES],
                )
                for weekday in WEEKDAYS_MF
                for session in Session
            ],
            admin_half_days_per_week=1,
        ),
        max_consecutive_weeks=6,
    )


def blank_instance(*, academic_year: str = SAMPLE_ACADEMIC_YEAR) -> SchedulerInput:
    """Create an editable workspace without bundled demonstration data."""
    first_week_start = first_week_start_for_academic_year(academic_year)
    academic = ClinicSlot(
        weekday=Weekday.WEDNESDAY,
        session=Session.AFTERNOON,
    )
    requirements = [PGYCurriculum(pgy=1, code="PGY1", label="PGY 1")]
    training_level_ids = tuple(item.pgy for item in requirements)
    return SchedulerInput(
        academic_year=academic_year,
        calendar=Calendar(
            weeks=52,
            first_week_start=first_week_start,
            block_start_alignment=1,
        ),
        residents=[],
        rotations=[
            _default_clinic_rotation(
                training_level_ids=training_level_ids,
                color=DEFAULT_COLOR_SCHEME.accents[4].color,
            ),
            _default_fmed_rotation(
                training_level_ids=training_level_ids,
                academic=academic,
                color=DEFAULT_COLOR_SCHEME.secondary.color,
            ),
        ],
        requirements=requirements,
        rotation_groups=[],
        electives=ElectiveConfiguration(),
        clinic_policy=ClinicPolicy(
            sites=[
                ClinicSiteConfig(
                    id="clinic",
                    name="Clinic",
                    color=DEFAULT_COLOR_SCHEME.accents[0].color,
                )
            ],
            primary_site_id="clinic",
            allocation_rules=[
                ClinicAllocationRule(
                    clinic_id="clinic",
                    min_percent=0,
                    target_percent=100,
                    max_percent=100,
                )
            ],
            academic=academic,
        ),
        solver=SolverConfig(),
    )


def current_blank_instance(*, today: date | None = None) -> SchedulerInput:
    """Create an empty workspace for the current academic year."""
    return blank_instance(academic_year=academic_year_for_date(today))


def sample_instance(
    catalog: ConstraintCatalog | None = None,
    *,
    academic_year: str = SAMPLE_ACADEMIC_YEAR,
) -> SchedulerInput:
    constraints = catalog or bundled_catalog()
    first_week_start = first_week_start_for_academic_year(SAMPLE_ACADEMIC_YEAR)
    residents = [
        resident.model_copy(
            update={
                "days_off": sample_days_off(resident.id, first_week_start),
                "elective_preferences": sample_elective_preferences(resident.id, resident.pgy),
            }
        )
        for resident in sample_residents()
    ]
    instance = SchedulerInput(
        academic_year=SAMPLE_ACADEMIC_YEAR,
        calendar=Calendar(
            weeks=52,
            first_week_start=first_week_start,
            block_start_alignment=1,
        ),
        residents=residents,
        attendings=sample_attendings(first_week_start),
        attending_schedules=sample_attending_schedules(),
        rotations=constraints.rotations,
        requirements=constraints.requirements,
        rotation_groups=constraints.rotation_groups,
        electives=constraints.electives,
        locks=sample_locks(constraints, residents),
        academic_half_day_overrides=sample_academic_overrides(),
        special_rotations=sample_special_rotations(first_week_start),
        clinic_policy=constraints.clinic_policy,
        solver=SolverConfig(),
    )
    return rebase_academic_year(instance, academic_year)


def current_sample_instance(*, today: date | None = None) -> SchedulerInput:
    """Create a new workspace for the current academic year."""
    return sample_instance(academic_year=academic_year_for_date(today))


def sample_locks(
    catalog: ConstraintCatalog | None = None,
    residents: list[Resident] | None = None,
) -> list[LockedPlacement]:
    """Create illustrative pins from available typed rules, without catalog IDs."""
    constraints = catalog or bundled_catalog()
    people = residents or sample_residents()
    rotations = {rotation.id: rotation for rotation in constraints.rotations}
    locks: list[LockedPlacement] = []

    first = people[0]
    first_curriculum = next(item for item in constraints.requirements if item.pgy == first.pgy)
    vacation_pair = next(
        (
            (week, week + 1)
            for week in first.vacation_weeks
            if week + 1 in first.vacation_weeks
        ),
        None,
    )
    clinic_block = next(
        (
            block
            for block in first_curriculum.blocks
            if rotations[block.rotation_id].kind is RotationKind.CLINIC
            and block.duration_weeks == 2
        ),
        None,
    )
    if clinic_block is not None and vacation_pair is not None:
        locks.append(
            LockedPlacement(
                resident_id=first.id,
                rotation_id=clinic_block.rotation_id,
                weeks=list(vacation_pair),
            )
        )

    second = next((resident for resident in people if resident.pgy != first.pgy), None)
    if second is not None:
        curriculum = next(
            item for item in constraints.requirements if item.pgy == second.pgy
        )
        managed_block = next(
            (
                block
                for block in curriculum.blocks
                if block.duration_weeks == 4
                and rotations[block.rotation_id].kind is RotationKind.FMED
            ),
            None,
        )
        if managed_block is not None:
            locks.append(
                LockedPlacement(
                    resident_id=second.id,
                    rotation_id=managed_block.rotation_id,
                    weeks=[1, 2, 3, 4],
                )
            )
    return locks
