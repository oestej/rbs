"""Minimal PyInstaller entry script for the native RBS Desktop application."""

import multiprocessing

# This must precede imports of the application and native UI stacks. PyInstaller
# uses command-line markers to turn spawned copies of the executable into
# multiprocessing workers rather than second application windows.
multiprocessing.freeze_support()

from rbs.desktop.main import main  # noqa: E402

raise SystemExit(main())
