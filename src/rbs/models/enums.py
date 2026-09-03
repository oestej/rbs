from enum import StrEnum


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


WEEKDAYS_MF = (
    Weekday.MONDAY,
    Weekday.TUESDAY,
    Weekday.WEDNESDAY,
    Weekday.THURSDAY,
    Weekday.FRIDAY,
)


class Session(StrEnum):
    MORNING = "morning"
    AFTERNOON = "afternoon"


class RotationKind(StrEnum):
    """How the residency treats this block. Kinds may have custom engine rules.

    ``standard`` — host service owns the day-to-day; we only overlay continuity clinic.
    ``clinic`` — dedicated clinic block; residency owns the daily template and clinic hours.
    ``fmed`` — inpatient teaching service; residency owns clinic afternoons.
    ``elective`` — uses standard placement rules but has a dedicated configuration surface.
    """

    STANDARD = "standard"
    CLINIC = "clinic"
    FMED = "fmed"
    ELECTIVE = "elective"


class SolverEngineName(StrEnum):
    STUB = "stub"
    CP_SAT = "cp_sat"


class SolverStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNKNOWN = "unknown"
    NOT_IMPLEMENTED = "not_implemented"
