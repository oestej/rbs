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
from rbs.models.catalog import ConstraintCatalog
from rbs.models.clinic import (
    ClinicAllocationRule,
    ClinicPolicy,
    ClinicSiteConfig,
    ClinicSlot,
)
from rbs.models.curriculum import PGYCurriculum
from rbs.models.elective import ElectiveConfiguration
from rbs.models.enums import RotationKind, Session, Weekday
from rbs.models.instance import (
    AcademicHalfDayOverride,
    Calendar,
    SchedulerInput,
    SolverConfig,
)
from rbs.models.locks import LockedPlacement
from rbs.models.resident import ElectivePreferenceRequest, Resident
from rbs.models.rotation import Rotation
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


def blank_instance(*, academic_year: str = SAMPLE_ACADEMIC_YEAR) -> SchedulerInput:
    """Create an editable workspace without bundled demonstration data."""
    first_week_start = first_week_start_for_academic_year(academic_year)
    return SchedulerInput(
        academic_year=academic_year,
        calendar=Calendar(
            weeks=52,
            first_week_start=first_week_start,
            block_start_alignment=1,
        ),
        residents=[],
        rotations=[],
        requirements=[PGYCurriculum(pgy=1, code="PGY1", label="PGY 1")],
        rotation_groups=[],
        electives=ElectiveConfiguration(),
        clinic_policy=ClinicPolicy(
            sites=[
                ClinicSiteConfig(
                    id="clinic",
                    name="Clinic",
                    color="#3971B8",
                )
            ],
            primary_site_id="clinic",
            allocation_rules=[
                ClinicAllocationRule(
                    clinic_id="clinic",
                    min_fraction=0.0,
                    target_fraction=1.0,
                    max_fraction=1.0,
                )
            ],
            academic=ClinicSlot(
                weekday=Weekday.WEDNESDAY,
                session=Session.AFTERNOON,
            ),
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
