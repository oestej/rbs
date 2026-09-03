"""Public resource planning for CP-SAT portfolio solves."""

from rbs.models.instance import SolverConfig

#: Below this, CP-SAT's portfolio loses the subsolvers this model needs; two
#: workers collapsed 5 of 6 measured runs to a near-useless schedule.
MIN_WORKERS_PER_ATTEMPT = 3


def portfolio_plan(config: SolverConfig) -> tuple[int, int]:
    """Split the worker budget into ``(attempts, workers per attempt)``."""
    workers = max(1, config.num_workers)
    attempts = max(1, config.solve_attempts)
    if attempts == 1:
        return 1, workers
    attempts = min(attempts, workers // MIN_WORKERS_PER_ATTEMPT)
    if attempts <= 1:
        return 1, workers
    return attempts, max(MIN_WORKERS_PER_ATTEMPT, workers // attempts)
