"""Elective-query helpers for SolverProblem (mixin)."""

from __future__ import annotations

from collections import Counter
from functools import cached_property

from rbs.models.enums import RotationKind
from rbs.models.rotation import Rotation


class ElectiveQueriesMixin:
    """Elective inventory, eligibility, and presentation helpers."""

    def elective_options_for(
        self,
        pgy: int,
        duration_weeks: int,
    ) -> tuple[Rotation, ...]:
        """Eligible services which can fill one Elective curriculum block."""
        return tuple(
            self.rotation(option.rotation_id)
            for option in self.electives.rotation_options
            if option.allows(pgy, duration_weeks)
            and self.rotation(option.rotation_id).allows_duration(
                duration_weeks,
                pgy=pgy,
            )
        )

    def is_elective_option(self, rotation_id: str) -> bool:
        return self.electives.option_for(rotation_id) is not None

    def direct_elective_block_counts_for_pgy(self, pgy: int) -> dict[int, int]:
        """Count direct Elective curriculum occurrences by block duration."""
        counts: Counter[int] = Counter()
        for block in self.curriculum_for(pgy).blocks:
            if self.rotation(block.rotation_id).kind is RotationKind.ELECTIVE:
                counts[block.duration_weeks] += block.count
        return dict(sorted(counts.items()))

    def elective_fallback_rotation(
        self,
        pgy: int,
        duration_weeks: int,
    ) -> Rotation | None:
        """Return the Clinic service able to absorb one Elective block shape."""
        return next(
            (
                rotation
                for rotation in self.rotations
                if rotation.kind is RotationKind.CLINIC
                and rotation.allows_duration(duration_weeks, pgy=pgy)
            ),
            None,
        )

    def is_elective_fallback_rotation(
        self,
        rotation_id: str,
        pgy: int,
        duration_weeks: int | None = None,
    ) -> bool:
        rotation = self.rotations_by_id.get(rotation_id)
        if rotation is None or rotation.kind is not RotationKind.CLINIC:
            return False
        durations = self.direct_elective_block_counts_for_pgy(pgy)
        if duration_weeks is not None:
            return duration_weeks in durations and rotation.allows_duration(duration_weeks, pgy=pgy)
        return any(rotation.allows_duration(duration, pgy=pgy) for duration in durations)

    @cached_property
    def elective_block_sizes(self) -> tuple[int, ...]:
        """All block sizes that occur in configured Elective curriculum time."""
        return tuple(
            sorted(
                {
                    duration
                    for curriculum in self.requirements
                    for duration in self.elective_block_durations_for_pgy(curriculum.pgy)
                }
            )
        )

    def eligible_elective_block_sizes(self, rotation_id: str) -> tuple[int, ...]:
        """Elective block sizes explicitly enabled for one service."""
        return self.electives.block_sizes_for(rotation_id)

    def eligible_elective_pgys(self, rotation_id: str) -> tuple[int, ...]:
        """Training levels explicitly allowed to take one service as an Elective."""
        return self.electives.pgys_for(rotation_id)

    def elective_option_is_repeatable(self, rotation_id: str) -> bool:
        """Whether one resident may take this service repeatedly as an Elective."""
        option = self.electives.option_for(rotation_id)
        return bool(option is not None and option.repeatable)

    def available_elective_pgys(self, rotation_id: str) -> tuple[int, ...]:
        """Training levels with a compatible direct Elective slot for this service."""
        rotation = self.rotation(rotation_id)
        return tuple(
            curriculum.pgy
            for curriculum in self.requirements
            if any(
                rotation.allows_duration(duration, pgy=curriculum.pgy)
                for duration in self.elective_block_durations_for_pgy(curriculum.pgy)
            )
        )

    def available_elective_block_sizes(self, rotation_id: str) -> tuple[int, ...]:
        """Elective curriculum sizes supported by this service for a matching level."""
        rotation = self.rotation(rotation_id)
        return tuple(
            duration
            for duration in self.elective_block_sizes
            if any(
                duration in self.elective_block_durations_for_pgy(curriculum.pgy)
                and rotation.allows_duration(duration, pgy=curriculum.pgy)
                for curriculum in self.requirements
            )
        )

    def assignment_color(self, rotation_id: str, *, elective: bool = False) -> str:
        """Resolve schedule color, including Elective inheritance semantics."""
        rotation = self.rotation(rotation_id)
        if elective and rotation.kind is RotationKind.ELECTIVE:
            return self.electives.color
        return rotation.color

    def assignment_name(self, rotation_id: str, *, elective: bool = False) -> str:
        rotation = self.rotation(rotation_id)
        name = rotation.name
        if elective and rotation.kind is RotationKind.CLINIC:
            return f"{name} (Elective fallback)"
        return f"{name} (Elec)" if elective else name

    def assignment_label(self, rotation_id: str, *, elective: bool = False) -> str:
        rotation = self.rotation(rotation_id)
        return f"{rotation.code} · {self.assignment_name(rotation_id, elective=elective)}"
