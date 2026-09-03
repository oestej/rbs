"""Top-level elective rotation policy."""

from pydantic import Field, field_validator, model_validator

from rbs.models.color_scheme import normalize_hex_color
from rbs.models.common import StrictModel
from rbs.models.curriculum import PGYCurriculum
from rbs.models.enums import RotationKind
from rbs.models.rotation import DEFAULT_ROTATION_COLOR, Rotation


class ElectiveRotationOption(StrictModel):
    """One service and the Elective slots it may fill."""

    rotation_id: str
    eligible_pgys: list[int] = Field(
        default_factory=list,
        description=(
            "Training levels for which this service may fill Elective curriculum "
            "time. An omitted value is populated from compatible curriculum blocks."
        ),
    )
    eligible_block_sizes: list[int] = Field(
        default_factory=list,
        description=(
            "Elective curriculum block sizes this service may fill. An omitted "
            "value is populated from compatible curriculum blocks."
        ),
    )
    repeatable: bool = Field(
        default=False,
        description=("Whether one resident may take this service more than once as an elective."),
    )

    @field_validator("rotation_id")
    @classmethod
    def normalize_rotation_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("elective option rotation ID cannot be empty")
        return normalized

    @field_validator("eligible_pgys")
    @classmethod
    def normalize_pgys(cls, values: list[int]) -> list[int]:
        normalized = sorted(int(value) for value in values)
        if any(value < 1 for value in normalized):
            raise ValueError("eligible elective training levels must be positive")
        if len(normalized) != len(set(normalized)):
            raise ValueError("eligible elective training levels must be unique")
        return normalized

    @field_validator("eligible_block_sizes")
    @classmethod
    def normalize_block_sizes(cls, values: list[int]) -> list[int]:
        normalized = sorted(int(value) for value in values)
        if any(value < 1 for value in normalized):
            raise ValueError("eligible elective block sizes must be positive")
        if len(normalized) != len(set(normalized)):
            raise ValueError("eligible elective block sizes must be unique")
        return normalized

    def allows(self, pgy: int, duration_weeks: int) -> bool:
        """Return whether this policy admits one training-level block shape."""
        return pgy in self.eligible_pgys and duration_weeks in self.eligible_block_sizes


class ElectiveConfiguration(StrictModel):
    """Shared presentation and the services eligible to fill elective time.

    Eligible IDs may point at either a standalone ``elective`` rotation or a
    normal Mandatory service, or FMED. Mandatory and FMED services keep their
    own color and consume the same capacity pool when selected as an elective.
    Standalone elective services use this configuration's shared color.
    """

    color: str = Field(
        default=DEFAULT_ROTATION_COLOR,
        description="Shared block-schedule color for standalone elective services.",
    )
    rotation_options: list[ElectiveRotationOption] = Field(
        default_factory=list,
        description="Services and block sizes which may fill Elective curriculum time.",
    )

    @field_validator("color")
    @classmethod
    def normalize_color(cls, value: str) -> str:
        return normalize_hex_color(value)

    @model_validator(mode="after")
    def unique_rotation_options(self) -> "ElectiveConfiguration":
        ids = self.eligible_rotation_ids
        if len(ids) != len(set(ids)):
            raise ValueError("eligible elective rotation IDs must be unique")
        return self

    @property
    def eligible_rotation_ids(self) -> list[str]:
        return [option.rotation_id for option in self.rotation_options]

    def option_for(self, rotation_id: str) -> ElectiveRotationOption | None:
        return next(
            (option for option in self.rotation_options if option.rotation_id == rotation_id),
            None,
        )

    def block_sizes_for(
        self,
        rotation_id: str,
        *,
        pgy: int | None = None,
    ) -> tuple[int, ...]:
        option = self.option_for(rotation_id)
        if option is None or (pgy is not None and pgy not in option.eligible_pgys):
            return ()
        return tuple(option.eligible_block_sizes)

    def pgys_for(self, rotation_id: str) -> tuple[int, ...]:
        option = self.option_for(rotation_id)
        return tuple(option.eligible_pgys) if option is not None else ()


# A longer compatibility name is useful to schema consumers without creating a
# second Pydantic type or a different serialized shape.
ElectiveRotationConfiguration = ElectiveConfiguration


__all__ = [
    "ElectiveConfiguration",
    "ElectiveRotationConfiguration",
    "ElectiveRotationOption",
]

def apply_elective_option_defaults(
    rotations: list[Rotation],
    requirements: list[PGYCurriculum],
    configuration: ElectiveConfiguration,
) -> ElectiveConfiguration:
    """Populate training levels and block sizes omitted by a policy.

    Defaults are limited to actual Elective curriculum shapes which the service
    can fill.
    """
    by_id = {rotation.id: rotation for rotation in rotations}
    shapes: set[tuple[int, int]] = set()
    source_ids: set[str] = set()
    for curriculum in requirements:
        pgy = curriculum.pgy
        for block in curriculum.blocks:
            source = by_id.get(block.rotation_id)
            if source is not None and source.kind is RotationKind.ELECTIVE:
                source_ids.add(source.id)
                shapes.add((pgy, int(block.duration_weeks)))

    options: list[ElectiveRotationOption] = []
    for option in configuration.rotation_options:
        # A curriculum Elective record is an internal slot marker. Older
        # policies exposed it as a general-purpose option; drop that stale
        # entry while preserving real standalone, Mandatory-backed, and FMED options.
        if option.rotation_id in source_ids:
            continue
        rotation = by_id.get(option.rotation_id)
        sizes = option.eligible_block_sizes or (
            sorted(
                {
                    duration
                    for pgy, duration in shapes
                    if rotation is not None and rotation.allows_duration(duration, pgy=pgy)
                }
            )
            if rotation is not None
            else []
        )
        pgys = option.eligible_pgys or (
            sorted(
                {
                    pgy
                    for pgy, duration in shapes
                    if duration in sizes
                    and rotation is not None
                    and rotation.allows_duration(duration, pgy=pgy)
                }
            )
            if rotation is not None
            else []
        )
        options.append(
            option.model_copy(
                update={
                    "eligible_pgys": pgys,
                    "eligible_block_sizes": sizes,
                }
            )
        )
    return configuration.model_copy(update={"rotation_options": options})


__all__.append("apply_elective_option_defaults")


def apply_shared_elective_color(
    rotations: list[Rotation],
    configuration: ElectiveConfiguration,
) -> list[Rotation]:
    """Return rotations with standalone Elective colors normalized to policy."""
    return [
        rotation.model_copy(update={"color": configuration.color})
        if rotation.kind is RotationKind.ELECTIVE and rotation.color != configuration.color
        else rotation
        for rotation in rotations
    ]


__all__.append("apply_shared_elective_color")
