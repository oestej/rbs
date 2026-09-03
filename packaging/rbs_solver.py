"""Console-capable solver helper bundled inside RBS Desktop."""

import multiprocessing

multiprocessing.freeze_support()

from rbs.solver.process import main  # noqa: E402

raise SystemExit(main())
