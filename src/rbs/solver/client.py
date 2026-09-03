"""Transport client for the standalone solver process."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from time import monotonic

from pydantic import ValidationError

from rbs.logging import current_runtime, get_logger, log_context, relay_solver_stderr
from rbs.models.instance import SolverConfig, SolverProblem
from rbs.models.schedule import Schedule
from rbs.solver.contract import SolveFailure, SolveRequest, parse_response_json

SOLVER_COMMAND_ENV = "RBS_SOLVER_COMMAND"
FROZEN_SOLVER_NAME = "rbs-solver"


class SolverProcessError(RuntimeError):
    """The solver process failed or violated its wire contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SolverProcessClient:
    """Send one request per child process using JSON over stdin/stdout.

    The default command uses the current Python environment.  A separately
    packaged solver binary can be selected explicitly or with
    ``RBS_SOLVER_COMMAND`` without changing UI code.
    """

    def __init__(self, command: Sequence[str] | None = None) -> None:
        resolved = tuple(command) if command is not None else default_solver_command()
        if not resolved:
            raise ValueError("solver command cannot be empty")
        self.command = resolved

    def solve(
        self,
        problem: SolverProblem,
        *,
        options: SolverConfig,
        reference_solution: Schedule | None = None,
        timeout: float | None = None,
    ) -> Schedule:
        request = SolveRequest.from_problem(
            problem,
            options=options,
            reference_solution=reference_solution,
        )
        logger = get_logger("solver.transport")
        started = monotonic()
        try:
            with log_context(solve_id=request.request_id):
                logger.info(
                    "solver.process_started",
                    engine=options.engine.value,
                    num_workers=options.num_workers,
                    time_limit_seconds=options.time_limit_seconds,
                )
                completed = subprocess.run(
                    self.command,
                    input=request.model_dump_json(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                    env=_child_environment(),
                )
        except subprocess.TimeoutExpired as exc:
            logger.error(
                "solver.process_timed_out",
                duration_ms=round((monotonic() - started) * 1000),
                error_code="timeout",
            )
            raise SolverProcessError("timeout", "solver process exceeded its deadline") from exc
        except OSError as exc:
            logger.error(
                "solver.process_failed",
                error_code="process_start_failed",
                exc_info=True,
            )
            raise SolverProcessError(
                "process_start_failed", "solver process could not be started"
            ) from exc
        if current_runtime() is not None:
            relay_solver_stderr(completed.stderr, exit_code=completed.returncode)
        with log_context(solve_id=request.request_id):
            try:
                solution = _read_solution(
                    request,
                    completed.stdout,
                    returncode=completed.returncode,
                    stderr=completed.stderr,
                )
            except SolverProcessError as exc:
                logger.error(
                    "solver.process_failed",
                    duration_ms=round((monotonic() - started) * 1000),
                    exit_code=completed.returncode,
                    error_code=exc.code,
                )
                raise
            logger.info(
                "solver.process_completed",
                duration_ms=round((monotonic() - started) * 1000),
                exit_code=completed.returncode,
            )
            return solution

    async def solve_async(
        self,
        problem: SolverProblem,
        *,
        options: SolverConfig,
        reference_solution: Schedule | None = None,
        timeout: float | None = None,
    ) -> Schedule:
        request = SolveRequest.from_problem(
            problem,
            options=options,
            reference_solution=reference_solution,
        )
        logger = get_logger("solver.transport")
        started = monotonic()
        with log_context(solve_id=request.request_id):
            logger.info(
                "solver.process_started",
                engine=options.engine.value,
                num_workers=options.num_workers,
                time_limit_seconds=options.time_limit_seconds,
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_environment(),
            )
        except OSError as exc:
            logger.error(
                "solver.process_failed",
                error_code="process_start_failed",
                exc_info=True,
            )
            raise SolverProcessError(
                "process_start_failed", "solver process could not be started"
            ) from exc

        try:
            communication = process.communicate(request.model_dump_json().encode("utf-8"))
            if timeout is None:
                stdout, stderr = await communication
            else:
                stdout, stderr = await asyncio.wait_for(communication, timeout=timeout)
        except TimeoutError as exc:
            await _stop_process(process)
            logger.error(
                "solver.process_timed_out",
                duration_ms=round((monotonic() - started) * 1000),
                error_code="timeout",
            )
            raise SolverProcessError("timeout", "solver process exceeded its deadline") from exc
        except asyncio.CancelledError:
            await _stop_process(process)
            logger.info(
                "solver.process_cancelled",
                duration_ms=round((monotonic() - started) * 1000),
            )
            raise
        if current_runtime() is not None:
            relay_solver_stderr(stderr, exit_code=process.returncode)
        with log_context(solve_id=request.request_id):
            try:
                solution = _read_solution(
                    request,
                    stdout,
                    returncode=process.returncode,
                    stderr=stderr,
                )
            except SolverProcessError as exc:
                logger.error(
                    "solver.process_failed",
                    duration_ms=round((monotonic() - started) * 1000),
                    exit_code=process.returncode,
                    error_code=exc.code,
                )
                raise
            logger.info(
                "solver.process_completed",
                duration_ms=round((monotonic() - started) * 1000),
                exit_code=process.returncode,
            )
            return solution


def default_solver_command() -> tuple[str, ...]:
    configured = os.environ.get(SOLVER_COMMAND_ENV)
    if configured is not None:
        command = tuple(shlex.split(configured))
        if not command:
            raise ValueError(f"{SOLVER_COMMAND_ENV} cannot be empty")
        return command
    if getattr(sys, "frozen", False):
        # A windowed executable has no usable stdin/stdout, so the JSON solver
        # protocol cannot re-enter it directly. The desktop bundle places a
        # console-capable helper beside the main executable instead.
        suffix = ".exe" if sys.platform == "win32" else ""
        helper = Path(sys.executable).with_name(f"{FROZEN_SOLVER_NAME}{suffix}")
        return (str(helper),)
    return (sys.executable, "-m", "rbs.solver")


def solution_from_response(
    request: SolveRequest,
    payload: str | bytes,
    *,
    returncode: int = 0,
    stderr: str | bytes = "",
) -> Schedule:
    """Decode a response from any transport, including a process pool."""
    return _read_solution(
        request,
        payload,
        returncode=returncode,
        stderr=stderr,
    )


def _read_solution(
    request: SolveRequest,
    payload: str | bytes,
    *,
    returncode: int,
    stderr: str | bytes,
) -> Schedule:
    try:
        response = parse_response_json(payload)
    except (ValidationError, ValueError) as exc:
        raise SolverProcessError(
            "invalid_response",
            f"solver returned an invalid response (exit {returncode})",
        ) from exc

    if response.request_id != request.request_id:
        raise SolverProcessError(
            "mismatched_response",
            "solver response request_id does not match the request",
        )
    if isinstance(response, SolveFailure):
        raise SolverProcessError(response.error.code, response.error.message)
    if returncode != 0:
        raise SolverProcessError(
            "process_failed",
            f"solver exited with status {returncode} after returning success",
        )
    return response.solution


def _child_environment() -> dict[str, str]:
    runtime = current_runtime()
    return dict(os.environ) if runtime is None else runtime.child_environment()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
