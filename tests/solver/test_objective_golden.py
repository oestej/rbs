"""Golden fingerprint for the clinic objective model build.

The ``objective.py`` split (slots / entries / terms / orchestration) must be a
verbatim code motion: compiling the same instance has to produce the exact
same CP-SAT proto, bounds, and decision counts. This test pins that behavior.

Regenerate ``objective_golden.json`` only intentionally, after reviewing the
model diff::

    RBS_REGENERATE_GOLDEN=1 .venv/bin/python -m pytest tests/solver/test_objective_golden.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rbs.catalog import sample_instance as unconfigured_sample_instance
from rbs.models.enums import Session, Weekday
from rbs.models.instance import SchedulerInput

GOLDEN_PATH = Path(__file__).with_name("objective_golden.json")


def _configured_instance() -> SchedulerInput:
    """Sample year with one explicitly configured test elective."""
    instance = unconfigured_sample_instance()
    raw = instance.model_dump(mode="json")
    source = next(rotation for rotation in raw["rotations"] if rotation["id"] == "elective")
    configured = {
        **source,
        "id": "configured_test_elective",
        "code": "T-ELEC",
        "name": "Configured Test Elective",
    }
    raw["rotations"].append(configured)
    raw["electives"]["rotation_options"] = [
        {
            "rotation_id": configured["id"],
            "eligible_block_sizes": [2],
        }
    ]
    return SchedulerInput.model_validate(raw)


def _academic_override_instance() -> SchedulerInput:
    """Configured sample with one academic half-day override (week 11)."""
    raw = _configured_instance().model_dump(mode="json")
    raw["academic_half_day_overrides"] = [
        {
            "week": 11,
            "weekday": Weekday.TUESDAY.value,
            "session": Session.MORNING.value,
        }
    ]
    return SchedulerInput.model_validate(raw)


def objective_fingerprint(instance: SchedulerInput) -> dict:
    """Compile one instance and summarize the resulting solver model."""
    from ortools.sat.python import cp_model

    from rbs.solver.core.compile import compile_problem

    compiled = compile_problem(instance, instance.solver, cp_model)
    proto = compiled.context.model.Proto()
    # Newer OR-Tools exposes the proto as a pybind11 wrapper without
    # SerializeToString; the text format is deterministic for identical
    # construction order, which is exactly what this golden pins.
    payload = str(proto).encode("utf-8")
    clinic = compiled.clinic
    return {
        "proto_sha256": hashlib.sha256(payload).hexdigest(),
        "num_variables": len(proto.variables),
        "num_constraints": len(proto.constraints),
        "quality_bound": int(clinic.quality_bound),
        "stability_comparisons": int(clinic.stability_comparisons),
        "has_objective": bool(clinic.has_objective),
        "num_decisions": len(clinic.decisions),
        "num_in_clinic_entries": sum(len(entries) for entries in clinic.in_clinic.values()),
        "num_attending_variables": len(clinic.attending_variables),
    }


def _fingerprints() -> dict:
    return {
        "configured_sample": objective_fingerprint(_configured_instance()),
        "academic_override": objective_fingerprint(_academic_override_instance()),
    }


def test_objective_model_matches_golden() -> None:
    import os

    actual = _fingerprints()
    if os.environ.get("RBS_REGENERATE_GOLDEN") == "1":
        GOLDEN_PATH.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN_PATH.read_text())
    assert actual == expected


def test_objective_submodules_reexport_compat() -> None:
    """Old import paths keep working after the split."""
    from rbs.solver.core import objective

    for name in (
        "add_clinic_objective",
        "_ClinicObjectiveState",
        "_Conditional",
        "_occupancy_floor",
        "_add_occupancy_floor",
        "_slot_literals",
        "_preferred_slot_penalties",
        "_quality_terms",
        "_finish_clinic_objective",
    ):
        assert callable(getattr(objective, name)) or isinstance(
            getattr(objective, name), type
        ), name

    from rbs.solver.core.objective_entries import _collect_week_entries
    from rbs.solver.core.objective_slots import _Conditional as SlotsConditional
    from rbs.solver.core.objective_terms import _quality_terms as TermsQuality

    assert objective._Conditional is SlotsConditional
    assert objective._collect_week_entries is _collect_week_entries
    assert objective._quality_terms is TermsQuality
