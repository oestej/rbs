from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from rbs import __version__
from rbs.catalog import bootstrap_catalog, sample_instance
from rbs.emit import write_json
from rbs.ingest import load_instance
from rbs.logging import LoggingConfig, configure_logging, get_logger
from rbs.models.catalog import ConstraintCatalog
from rbs.models.enums import SolverStatus
from rbs.models.instance import SchedulerInput, SchedulingCase
from rbs.models.schedule import Schedule
from rbs.runner import run_schedule
from rbs.summary import summarize


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rbs",
        description="Residency block scheduler: ingest JSON, emit a JSON schedule.",
    )
    parser.add_argument("--version", action="version", version=f"rbs {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="Load and validate an instance JSON file")
    _add_input_args(validate)

    schedule = sub.add_parser("schedule", help="Ingest an instance and write a schedule JSON")
    _add_input_args(schedule)
    schedule.add_argument(
        "-o",
        "--output",
        default="schedule.json",
        help="Schedule output path (default: schedule.json)",
    )
    schedule.add_argument(
        "--engine",
        choices=["stub", "cp_sat"],
        default=None,
        help="Solver engine (default: value in the instance, usually cp_sat)",
    )

    schema = sub.add_parser("schema", help="Print JSON Schema for input or output")
    schema.add_argument("kind", choices=["input", "case", "catalog", "output"])

    dump = sub.add_parser("dump-sample", help="Write the bundled sample instance to a JSON file")
    dump.add_argument(
        "-o",
        "--output",
        default="data/sample_input.json",
        help="Destination path (default: data/sample_input.json)",
    )

    catalog = sub.add_parser("dump-catalog", help="Write the default rotation catalog to JSON")
    catalog.add_argument(
        "-o",
        "--output",
        default="data/catalog.json",
        help="Destination path (default: data/catalog.json)",
    )

    ui_cmd = sub.add_parser("ui", help="Open the NiceGUI workspace (SQLite + JSON import/export)")
    ui_cmd.add_argument("--db", default="rbs.sqlite", help="SQLite path (default: rbs.sqlite)")
    ui_cmd.add_argument("--host", default="127.0.0.1")
    ui_cmd.add_argument("--port", type=int, default=8080)
    ui_cmd.add_argument("--reload", action="store_true")
    ui_cmd.add_argument("--no-browser", action="store_true", help="Do not open a browser window")
    packaging = ui_cmd.add_mutually_exclusive_group()
    packaging.add_argument(
        "--desktop",
        action="store_true",
        help="Preview desktop chrome locally; file actions are inert",
    )
    packaging.add_argument(
        "--cloud",
        action="store_true",
        help="Preview hosted chrome locally; still single-user with no retention",
    )

    return parser


def _add_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="Path to instance JSON")
    parser.add_argument(
        "--catalog",
        default=None,
        help="Optional catalog JSON used when the instance omits rotations/requirements",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = configure_logging(
        LoggingConfig(
            runtime="local" if args.command == "ui" else "cli",
            component="ui" if args.command == "ui" else "cli",
            destination="stdout" if args.command == "ui" else "stderr",
        )
    )
    logger = get_logger("cli")
    logger.info("application.started")
    try:
        try:
            result = _dispatch(args)
        except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            logger.error(
                "cli.command_failed",
                error_code=type(exc).__name__,
                exc_info=True,
            )
            print(f"error: {exc}", file=sys.stderr)
            result = 1
        except Exception:
            logger.exception(
                "cli.command_failed",
                error_code="unexpected_error",
            )
            print("error: an unexpected internal failure occurred", file=sys.stderr)
            result = 1
        logger.info("application.stopped", exit_code=result)
        return result
    finally:
        runtime.close()


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "validate":
        instance = load_instance(args.input, catalog_path=args.catalog)
        print(summarize(instance))
        print("ok")
        return 0

    if args.command == "schedule":
        schedule = run_schedule(
            args.input,
            args.output,
            catalog_path=args.catalog,
            engine=args.engine,
        )
        print(f"wrote {args.output} ({schedule.meta.status.value}, engine={schedule.meta.engine})")
        for note in schedule.meta.notes:
            print(f"note: {note}")
        for warning in schedule.meta.validation_warnings:
            print(f"warning: {warning}")
        for error in schedule.meta.validation_errors:
            print(f"error: {error}", file=sys.stderr)
        if schedule.meta.status in {SolverStatus.INFEASIBLE, SolverStatus.UNKNOWN}:
            return 2
        return 0

    if args.command == "schema":
        models = {
            "input": SchedulerInput,
            "case": SchedulingCase,
            "catalog": ConstraintCatalog,
            "output": Schedule,
        }
        model = models[args.kind]
        print(json.dumps(model.model_json_schema(), indent=2))
        return 0

    if args.command == "dump-sample":
        path = write_json(sample_instance().scheduling_case(), args.output)
        print(f"wrote {path}")
        return 0

    if args.command == "dump-catalog":
        destination = write_json(bootstrap_catalog(), args.output)
        print(f"wrote {destination}")
        return 0

    if args.command == "ui":
        from rbs.store import Store
        from rbs.ui.app import run_app
        from rbs.ui.preview import mode_from_flags

        run_app(
            Store(args.db),
            host=args.host,
            port=args.port,
            reload=args.reload,
            show=not args.no_browser,
            mode=mode_from_flags(desktop=args.desktop, cloud=args.cloud),
        )
        return 0

    raise AssertionError(f"unhandled command {args.command}")
