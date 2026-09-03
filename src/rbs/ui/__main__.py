"""``python -m rbs.ui`` entry point (composition root, owns Store construction)."""

from __future__ import annotations

import argparse

from rbs.store import Store
from rbs.ui.app import DEFAULT_DB, run_app


def main(argv: list[str] | None = None) -> int:
    from rbs.ui.preview import mode_from_flags

    parser = argparse.ArgumentParser(prog="rbs ui", description="NiceGUI workspace")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    packaging = parser.add_mutually_exclusive_group()
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
    args = parser.parse_args(argv)
    run_app(
        Store(args.db),
        host=args.host,
        port=args.port,
        reload=args.reload,
        show=not args.no_browser,
        mode=mode_from_flags(desktop=args.desktop, cloud=args.cloud),
    )
    return 0


raise SystemExit(main())
