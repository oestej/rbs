"""Solving off the event loop, bounded, and within limits the host sets."""

from __future__ import annotations

import asyncio
import os
import sys

from rbs.catalog import sample_instance
from rbs.cloud.config import CloudConfig
from rbs.cloud.solve_pool import SolvePool, clamp_solver_settings
from rbs.models.enums import SolverEngineName
from rbs.solver.client import SolverProcessClient
from rbs.store import Store
from rbs.ui.host import LocalHost


def _config(**overrides) -> CloudConfig:
    settings = {
        "cf_team_domain": "acme.cloudflareaccess.com",
        "cf_audience": "aud-for-this-app",
        "storage_secret": "signing-secret",
        "bootstrap_subjects": ("subject-a",),
    }
    settings.update(overrides)
    return CloudConfig(**settings)


def _stub_instance():
    instance = sample_instance()
    return instance.model_copy(
        update={"solver": instance.solver.model_copy(update={"engine": SolverEngineName.STUB})}
    )


def test_the_ceiling_is_the_hosts_to_set_not_the_users() -> None:
    config = _config(solve_ceiling_seconds=45.0, solve_workers=2)
    instance = sample_instance()
    greedy = instance.model_copy(
        update={
            "solver": instance.solver.model_copy(
                update={"time_limit_seconds": 100_000.0, "num_workers": 999}
            )
        }
    )

    bounded = clamp_solver_settings(greedy, config)

    assert bounded.solver.time_limit_seconds == 45.0
    assert bounded.solver.num_workers == 2


def test_clamping_leaves_everything_else_untouched() -> None:
    config = _config(solve_ceiling_seconds=45.0, solve_workers=2)
    instance = sample_instance()

    bounded = clamp_solver_settings(instance, config)

    assert bounded.solver.weights == instance.solver.weights
    assert bounded.solver.solve_attempts == instance.solver.solve_attempts
    assert bounded.residents == instance.residents


def test_the_pool_size_never_oversubscribes_the_box() -> None:
    config = _config(solve_pool_size=0, solve_workers=4)

    assert 1 <= SolvePool(config).size <= max(1, (__import__("os").cpu_count() or 1))


def test_nothing_is_queued_when_nothing_is_running() -> None:
    assert SolvePool(_config()).waiting == 0


def test_a_solve_runs_out_of_process_and_comes_back() -> None:
    pool = SolvePool(_config(solve_pool_size=1))

    async def run():
        try:
            return await pool.solve(_stub_instance())
        finally:
            await pool.shutdown()

    schedule = asyncio.run(run())

    assert schedule.meta.academic_year == sample_instance().academic_year


def test_the_desktop_host_solves_through_the_standalone_process(tmp_path) -> None:
    """The local build uses one child per solve rather than a shared pool."""
    host = LocalHost(Store(tmp_path / "local.sqlite"))

    schedule = asyncio.run(host.solve(host.principal(None), _stub_instance()))

    assert schedule.meta.academic_year == sample_instance().academic_year


def test_a_second_caller_waits_rather_than_oversubscribing() -> None:
    pool = SolvePool(_config(solve_pool_size=1))

    async def run():
        try:
            return await asyncio.gather(
                pool.solve(_stub_instance()),
                pool.solve(_stub_instance()),
            )
        finally:
            await pool.shutdown()

    first, second = asyncio.run(run())

    assert first is not None and second is not None
    # Both are done, so nobody is left queued and the accounting balanced.
    assert pool.waiting == 0


def test_a_solve_really_leaves_this_process() -> None:
    """The bounded pool delegates to a killable standalone solver process."""
    command = (
        sys.executable,
        "-c",
        (
            "import os,sys; from rbs.solver.service import handle_request_json; "
            "response=handle_request_json(sys.stdin.read()); "
            "response.solution.meta.notes=[str(os.getpid())]; "
            "print(response.model_dump_json())"
        ),
    )
    pool = SolvePool(
        _config(solve_pool_size=1),
        solver=SolverProcessClient(command=command),
    )

    async def run():
        try:
            return await pool.solve(_stub_instance())
        finally:
            await pool.shutdown()

    schedule = asyncio.run(run())

    assert int(schedule.meta.notes[0]) != os.getpid()


def test_deadline_does_not_scale_with_raw_portfolio_attempts() -> None:
    class RecordingClient(SolverProcessClient):
        def __init__(self) -> None:
            super().__init__()
            self.timeout = None

        async def solve_async(self, *args, timeout=None, **kwargs):
            self.timeout = timeout
            return await super().solve_async(*args, timeout=timeout, **kwargs)

    client = RecordingClient()
    pool = SolvePool(_config(solve_pool_size=1), solver=client)
    instance = _stub_instance()
    instance = instance.model_copy(
        update={
            "solver": instance.solver.model_copy(
                update={"time_limit_seconds": 7.0, "solve_attempts": 1_000_000}
            )
        }
    )

    async def run():
        try:
            return await pool.solve(instance)
        finally:
            await pool.shutdown()

    asyncio.run(run())

    assert client.timeout == 74.0
