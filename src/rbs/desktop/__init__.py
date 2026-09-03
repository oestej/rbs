"""Native desktop packaging for RBS.

The desktop entry point intentionally stays separate from :mod:`rbs.cli` and
the hosted entry point.  Importing this package must remain cheap: PyInstaller
and multiprocessing both import it while deciding which process role to run.
"""

