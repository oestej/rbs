from rbs.models.enums import SolverEngineName
from rbs.solver.core.base import SchedulerEngine
from rbs.solver.core.cp_sat import CpSatEngine
from rbs.solver.core.stub import StubEngine

_ENGINES: dict[SolverEngineName, type] = {
    SolverEngineName.STUB: StubEngine,
    SolverEngineName.CP_SAT: CpSatEngine,
}


def get_engine(name: str | SolverEngineName) -> SchedulerEngine:
    try:
        engine_name = SolverEngineName(name)
    except ValueError as exc:
        known = ", ".join(item.value for item in SolverEngineName)
        raise ValueError(f"unknown engine {name!r}; expected one of: {known}") from exc
    return _ENGINES[engine_name]()


__all__ = ["SchedulerEngine", "CpSatEngine", "StubEngine", "get_engine"]
