"""Public, portable API for the RBS solver.

``rbs.solver.core`` is private implementation detail. Applications should use
the models and entry points exported here or the JSON process protocol.
"""

from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule, SolverDiagnostic
from rbs.solver.client import SolverProcessClient, SolverProcessError
from rbs.solver.contract import (
    SOLVE_PROTOCOL,
    SOLVE_PROTOCOL_VERSION,
    SolveFailure,
    SolveRequest,
    SolveResponse,
    SolveSuccess,
)


def solve_problem(
    problem: SolverProblem,
    *,
    options: SolverConfig,
    reference_solution: Schedule | None = None,
) -> Schedule:
    """Lazily enter the solver implementation through the public API.

    Importing ``rbs.solver.validation`` first initializes this package. Keeping
    the implementation import inside the call prevents UI-only and frozen
    desktop processes from loading OR-Tools; the bundled solver helper still
    imports the service directly.
    """
    from rbs.solver.service import solve_problem as run

    return run(
        problem,
        options=options,
        reference_solution=reference_solution,
    )

__all__ = [
    "SOLVE_PROTOCOL",
    "SOLVE_PROTOCOL_VERSION",
    "SolveFailure",
    "SolveRequest",
    "SolveResponse",
    "SolveSuccess",
    "SolverConfig",
    "SolverDiagnostic",
    "SolverProblem",
    "SolverProcessClient",
    "SolverProcessError",
    "Schedule",
    "solve_problem",
]
