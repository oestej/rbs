import hashlib

from pydantic import Field, field_validator, model_validator

from rbs.models.clinic import (
    ALL_CLINIC_SITES,
    ClinicAllocationRule,
    ClinicCapacityOverride,
    ClinicClosureDay,
    ClinicHalfDayCapacity,
    ClinicPolicy,
    ClinicRule,
    ClinicSiteClosure,
    ClinicSiteConfig,
    ClinicSlot,
    clinic_slot_date,
    lighten_hex_color,
    normalize_clinic_site_ids,
)
from rbs.models.color_scheme import DEFAULT_COLOR_SCHEME, normalize_hex_color
from rbs.models.common import StrictModel
from rbs.models.enums import RotationKind

ROTATION_CODE_MAX_LENGTH = 6

# Compatibility exports for callers that need the bundled defaults. Runtime
# selectors use the palette on the current workspace's ColorScheme.
ROTATION_COLOR_PALETTE = tuple(
    item.color for item in DEFAULT_COLOR_SCHEME.selectable_colors
)
DEFAULT_ROTATION_COLOR = DEFAULT_COLOR_SCHEME.neutral.color


def default_rotation_color(rotation_id: str) -> str:
    """Choose a stable palette color for records that omit one."""
    if not rotation_id:
        return DEFAULT_ROTATION_COLOR
    digest = hashlib.sha256(rotation_id.encode()).digest()
    return ROTATION_COLOR_PALETTE[digest[0] % len(ROTATION_COLOR_PALETTE)]


class CapacityRule(StrictModel):
    min_concurrent: int | None = Field(default=None, ge=0)
    max_concurrent: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def min_not_above_max(self) -> "CapacityRule":
        if (
            self.min_concurrent is not None
            and self.max_concurrent is not None
            and self.min_concurrent > self.max_concurrent
        ):
            raise ValueError("min_concurrent cannot exceed max_concurrent")
        return self


class VacationRule(StrictModel):
    allowed: bool = False
    max_weeks_per_block: int | None = Field(
        default=None,
        ge=1,
        description="Cap on vacation weeks that may fall inside one placement of this block.",
    )

    @model_validator(mode="after")
    def default_allowed_vacation_limit(self) -> "VacationRule":
        if self.allowed and self.max_weeks_per_block is None:
            self.max_weeks_per_block = 1
        return self


class RotationBlockConfig(StrictModel):
    """One legal block shape for a resident in a specific training level."""

    duration_weeks: int = Field(ge=1, le=5)
    vacation: VacationRule = Field(default_factory=VacationRule)

    @model_validator(mode="after")
    def vacation_limit_fits_block(self) -> "RotationBlockConfig":
        maximum = self.vacation.max_weeks_per_block
        if not self.vacation.allowed and maximum is not None:
            raise ValueError("non-vacationable block cannot set a vacation-week maximum")
        if maximum is not None and maximum > self.duration_weeks:
            raise ValueError("vacation-week maximum cannot exceed block duration")
        return self


class PGYRotationRule(StrictModel):
    """Availability, placement, staffing, and block shapes for one training level."""

    pgy: int = Field(ge=1)
    min_concurrent: int | None = Field(default=None, ge=0)
    max_concurrent: int | None = Field(default=None, ge=0)
    max_total_weeks: int | None = Field(
        default=None,
        ge=1,
        le=52,
        description=(
            "Cap on total academic-year weeks one resident in this level may spend on "
            "the rotation."
        ),
    )
    prerequisite_rotation_ids: list[str] = Field(
        default_factory=list,
        description=(
            "At least one complete block of every listed rotation must finish before this "
            "rotation starts. The editor presents rotation codes; stable system IDs are stored."
        ),
    )
    earliest_start_week: int | None = Field(
        default=None,
        ge=1,
        le=52,
        description=(
            "First academic week of the earliest four-week block in which this rotation "
            "may start. Stored as a week number for the solver."
        ),
    )
    block_configs: list[RotationBlockConfig] = Field(min_length=1)

    @field_validator("prerequisite_rotation_ids")
    @classmethod
    def normalize_prerequisites(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("prerequisite rotation IDs cannot be empty")
        return normalized

    @model_validator(mode="after")
    def check_rule(self) -> "PGYRotationRule":
        if (
            self.min_concurrent is not None
            and self.max_concurrent is not None
            and self.min_concurrent > self.max_concurrent
        ):
            raise ValueError("min_concurrent cannot exceed max_concurrent")
        durations = [config.duration_weeks for config in self.block_configs]
        if len(durations) != len(set(durations)):
            raise ValueError(
                f"training-level key {self.pgy} block durations must be unique"
            )
        if len(self.prerequisite_rotation_ids) != len(set(self.prerequisite_rotation_ids)):
            raise ValueError(
                f"training-level key {self.pgy} prerequisite rotations must be unique"
            )
        return self


class Rotation(StrictModel):
    id: str
    code: str = Field(
        min_length=1,
        max_length=ROTATION_CODE_MAX_LENGTH,
        description="Short uppercase display code; spaces count toward the six-character limit.",
    )
    name: str
    color: str = Field(
        default=DEFAULT_ROTATION_COLOR,
        description="Palette color used for this rotation in block schedules.",
    )
    kind: RotationKind = Field(
        default=RotationKind.STANDARD,
        description="Dispatches custom engine rules. Not the same as rotation id.",
    )
    pgy_rules: list[PGYRotationRule] = Field(
        min_length=1,
        description=(
            "Training-level-specific staffing limits and legal block configurations."
        ),
    )
    clinic: ClinicRule | None = None
    capacity: CapacityRule = Field(default_factory=CapacityRule)
    away: bool = Field(
        default=False,
        description="Away from home; has no local clinic or academic sessions.",
    )
    no_clinic_hours: bool = Field(
        default=False,
        description="Suppresses all clinic sessions while preserving their configuration.",
    )
    no_weekend_call: bool = Field(
        default=False,
        description="Stored for future weekend-call scheduling rules.",
    )
    max_consecutive_weeks: int = Field(
        default=4,
        ge=1,
        le=6,
        description="Cap on consecutive weeks on this rotation.",
    )
    max_total_weeks: int | None = Field(
        default=None,
        ge=1,
        le=52,
        description=(
            "Cap on total academic-year weeks one resident may spend on this rotation."
        ),
    )

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper()
            if not value:
                raise ValueError("rotation code cannot be empty")
        return value

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return normalize_hex_color(value)

    @field_validator("id", "name")
    @classmethod
    def text_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value.strip():
            raise ValueError("rotation id and name cannot be empty")
        return value

    @model_validator(mode="after")
    def normalize_and_check(self) -> "Rotation":
        if self.away or self.clinic is None:
            self.no_clinic_hours = True
        pgys = [rule.pgy for rule in self.pgy_rules]
        if len(pgys) != len(set(pgys)):
            raise ValueError(f"{self.id}: training-level rules must have unique keys")
        return self

    @property
    def clinic_hours_disabled(self) -> bool:
        """Whether clinic must be omitted, including for unvalidated model copies."""
        return self.away or self.no_clinic_hours

    def pgy_rule(self, pgy: int) -> PGYRotationRule:
        for rule in self.pgy_rules:
            if rule.pgy == pgy:
                return rule
        raise KeyError(f"{self.id} has no rule for training-level key {pgy}")

    def max_total_weeks_for_pgy(self, pgy: int) -> int | None:
        """Return the tighter of the rotation-wide and training-level limits."""
        limits = [
            limit
            for limit in (self.max_total_weeks, self.pgy_rule(pgy).max_total_weeks)
            if limit is not None
        ]
        return min(limits) if limits else None

    def configured_durations(self, pgy: int | None = None) -> list[int]:
        rules = self.pgy_rules if pgy is None else [self.pgy_rule(pgy)]
        return sorted({config.duration_weeks for rule in rules for config in rule.block_configs})

    def allows_duration(self, duration_weeks: int, *, pgy: int) -> bool:
        try:
            rule = self.pgy_rule(pgy)
        except KeyError:
            return False
        return any(config.duration_weeks == duration_weeks for config in rule.block_configs)

    def block_config(self, pgy: int, duration_weeks: int) -> RotationBlockConfig:
        rule = self.pgy_rule(pgy)
        for config in rule.block_configs:
            if config.duration_weeks == duration_weeks:
                return config
        raise KeyError(
            f"{self.id} has no {duration_weeks}-week block configuration "
            f"for training-level key {pgy}"
        )

    @property
    def residency_managed(self) -> bool:
        """Day-to-day roster is owned by the residency, not a host rotation."""
        return self.kind in {RotationKind.CLINIC, RotationKind.FMED}

    @property
    def requires_dedicated_configuration(self) -> bool:
        """Whether this rotation should stay out of the generic host-service editor."""
        return self.kind in {
            RotationKind.CLINIC,
            RotationKind.FMED,
            RotationKind.ELECTIVE,
        }


__all__ = [
    "ALL_CLINIC_SITES",
    "DEFAULT_ROTATION_COLOR",
    "ROTATION_CODE_MAX_LENGTH",
    "ROTATION_COLOR_PALETTE",
    "CapacityRule",
    "ClinicAllocationRule",
    "ClinicCapacityOverride",
    "ClinicClosureDay",
    "ClinicHalfDayCapacity",
    "ClinicPolicy",
    "ClinicRule",
    "ClinicSiteClosure",
    "ClinicSiteConfig",
    "ClinicSlot",
    "PGYRotationRule",
    "Rotation",
    "RotationBlockConfig",
    "VacationRule",
    "clinic_slot_date",
    "default_rotation_color",
    "lighten_hex_color",
    "normalize_clinic_site_ids",
]
