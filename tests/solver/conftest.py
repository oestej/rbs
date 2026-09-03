"""Separates the CP-SAT search tests from the model-construction tests.

A search test asserts on what an anytime solver returns inside a wall-clock
budget, so its result depends on how much CPU the machine can spare. Hosted
runners have ~4 vCPU against a development machine's 16+, and the solver
defaults ask for eight workers across three concurrent attempts: the same
thirty seconds buys a fraction of the search there. Measured on
ubuntu-latest/macos-latest, the shared ``draft`` solve returned nothing
feasible and the quality assertions below it errored en masse.

Every test that starts a search carries the ``solve`` marker, short ones
included: a toy instance solved to proven optimality is cheap today, but it is
still the solver's answer being asserted on and it belongs with the rest.
CI deselects them with ``-m "not solve"``; they stay a local and pre-release
gate, run on real hardware.

What is left behind is deterministic and stays in CI: model compilation, the
golden proto fingerprint, the clinic-site allocator, planning helpers, and the
solver process contract, which runs the stub engine.
"""

import pytest


def pytest_collection_modifyitems(items) -> None:
    """Mark everything that depends on the shared full-year draft solve."""
    for item in items:
        if "draft" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.solve)
