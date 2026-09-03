"""Solver tuning value objects (weights and run configuration)."""

from __future__ import annotations

from pydantic import Field, model_validator

from rbs.models.common import StrictModel
from rbs.models.enums import SolverEngineName


class ObjectiveWeights(StrictModel):
    """Relative cost of each competing clinic goal.

    Only ratios matter. ``attending_sessions`` counts half-day attending shifts
    the primary clinic must staff across the year. Most other terms measure a
    spread (max - min); ``preferred_clinic_slots`` counts rotation blocks that
    had to use a non-preferred allowed clinic half-day.
    """

    attending_sessions: int = Field(
        default=300,
        ge=0,
        description="Attending half-day shifts the primary clinic must staff across the year.",
    )
    preferred_clinic_slots: int = Field(
        default=100,
        ge=0,
        description=(
            "Penalty for one rotation block using a non-preferred allowed clinic half-day. "
            "A higher value honors preferred times more strongly; zero disables the goal."
        ),
    )
    clinic_block_week_evenness: int = Field(
        default=20000,
        ge=0,
        description=(
            "Spread of how many residents sit on a dedicated Clinic block each "
            "week, summed across every training level. This is the lever against weeks "
            "where a crowd of residents lands on Clinic at once."
        ),
    )
    clinic_kind_pgy_spread: int = Field(
        default=1000,
        ge=0,
        description=(
            "Spread, within each training level, of residents on a dedicated Clinic block "
            "per week. Raising this stops one cohort's Clinic blocks bunching."
        ),
    )
    within_week_evenness: int = Field(
        default=40,
        ge=0,
        description="Spread of clinic load across the half-days inside one week.",
    )
    primary_site_week_evenness: int = Field(
        default=60,
        ge=0,
        description="Spread of the primary clinic's attending count per week.",
    )
    session_pgy_mix: int = Field(
        default=0,
        ge=0,
        description=(
            "Preference for mixing training years within a clinic session. Off by "
            "default: it adds a variable per session per year for a term worth a "
            "fraction of a percent, and measurably crowds out the evenness goals."
        ),
    )


class SolverConfig(StrictModel):
    engine: SolverEngineName = SolverEngineName.CP_SAT
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    time_limit_seconds: float = Field(default=60.0, gt=0)
    num_workers: int = Field(default=8, ge=1)
    relative_gap: float | None = Field(
        default=0.05,
        ge=0.0,
        description="Stop when the incumbent is within this relative gap of the bound.",
    )
    random_seed: int | None = None
    solve_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Independent solves to race against each other, keeping the best. "
            "The search lands in one of two basins roughly at random, so racing a "
            "few seeds reliably finds the good one. They run concurrently and "
            "share num_workers, so this costs no extra wall time."
        ),
    )
    allow_blocks_to_span_four_week_boundaries: bool = Field(
        default=False,
        description=(
            "Allow rotation blocks to cross the boundaries between the thirteen "
            "four-week academic blocks."
        ),
    )
    auto_balance_clinic_blocks: bool = Field(
        default=True,
        description=(
            "Spread dedicated Clinic blocks evenly across the calendar using a "
            "band derived from the curriculum. An explicit floor or ceiling below "
            "overrides it. Without a floor the solver empties whole weeks, because "
            "one resident alone in a session still needs a full attending."
        ),
    )
    min_clinic_blocks_per_week: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Hard floor on residents sitting on a dedicated Clinic block in any "
            "week. Overrides the automatic band."
        ),
    )
    max_clinic_blocks_per_week: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Hard ceiling on residents sitting on a dedicated Clinic block in "
            "any week. Overrides the automatic band."
        ),
    )

    @model_validator(mode="after")
    def clinic_block_bounds_are_ordered(self) -> SolverConfig:
        low, high = self.min_clinic_blocks_per_week, self.max_clinic_blocks_per_week
        if low is not None and high is not None and low > high:
            raise ValueError("min_clinic_blocks_per_week cannot exceed max_clinic_blocks_per_week")
        return self
