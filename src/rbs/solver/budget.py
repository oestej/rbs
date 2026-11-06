"""Shared wall-clock policy; independent of the solver implementation."""
from __future__ import annotations

import time
from dataclasses import dataclass


class BudgetExpired(RuntimeError):
    """No time remains to build or search a model (not infeasibility)."""


def remaining(deadline: float | None, *, cap: float) -> float:
    return cap if deadline is None else max(0.0, min(cap, deadline - time.perf_counter()))


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.perf_counter() >= deadline:
        raise BudgetExpired("solve time exhausted")


def process_timeout(time_limit_seconds: float) -> float:
    """Search plus bounded process startup, serialization, and shutdown allowance."""
    return time_limit_seconds + 30.0


@dataclass(frozen=True)
class SolveBudget:
    started: float
    seconds: float

    @property
    def deadline(self) -> float:
        return self.started + self.seconds

    def search_deadline(self, *, attendings: bool) -> float:
        return self.started + self.seconds * (0.8 if attendings else 1.0)

    def first_attempt_deadline(self, search_deadline: float, *, fallback: bool) -> float:
        # Reserve a meaningful retry even if the automatic band is hard to disprove.
        return (self.started + (search_deadline - self.started) * 0.7
                if fallback else search_deadline)

    def diagnostic_deadline(self) -> float:
        return min(self.deadline, time.perf_counter() + min(5.0, self.seconds * 0.1))
