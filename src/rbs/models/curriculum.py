import re

from pydantic import Field, field_validator, model_validator

from rbs.models.common import StrictModel


def default_training_level_code(pgy: int) -> str:
    """Return the legacy compact label for an otherwise unnamed level."""
    return f"PGY{pgy}"


def default_training_level_name(pgy: int) -> str:
    """Return the legacy descriptive label for an otherwise unnamed level."""
    return f"PGY {pgy}"


class BlockRequirement(StrictModel):
    """Required quantity for one rotation/block shape in a training-level curriculum."""

    rotation_id: str
    duration_weeks: int = Field(ge=1, le=5)
    count: int = Field(default=1, ge=1)


class RotationGroup(StrictModel):
    """An unordered, contiguous set of Mandatory rotations for one training level."""

    pgy: int = Field(ge=1)
    rotation_ids: list[str] = Field(min_length=2)

    @field_validator("rotation_ids")
    @classmethod
    def normalized_unique_rotation_ids(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("rotation group IDs cannot be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("rotation group members must be unique")
        return normalized


class PGYCurriculum(StrictModel):
    """One configured training level and its complete block curriculum.

    ``pgy`` remains the stable numeric key used by existing workspace files. The
    editable code and label are what users see, so the level can represent PGY4+,
    a fellow, or any other program-specific track without changing scheduling
    semantics.
    """

    pgy: int = Field(ge=1)
    code: str | None = Field(default=None, max_length=5)
    label: str | None = Field(default=None, max_length=80)
    blocks: list[BlockRequirement] = Field(default_factory=list)

    @field_validator("code", mode="before")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("training-level code cannot be empty")
        if not re.fullmatch(r"[A-Z0-9-]+", normalized):
            raise ValueError(
                "training-level code may contain only letters, numbers, and hyphens"
            )
        return normalized

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("training-level label cannot be empty")
        return normalized

    @model_validator(mode="after")
    def short_code_fits_compact_views(self) -> "PGYCurriculum":
        if len(self.short_code) > 5:
            raise ValueError(
                "training-level code must be set to 5 characters or fewer"
            )
        return self

    @property
    def display_label(self) -> str:
        return self.label or default_training_level_name(self.pgy)

    @property
    def short_code(self) -> str:
        return self.code or default_training_level_code(self.pgy)

    @property
    def compact_label(self) -> str:
        """Compatibility alias for callers that need the short identifier."""
        return self.short_code

    def required_weeks(self) -> int:
        return sum(item.duration_weeks * item.count for item in self.blocks)
