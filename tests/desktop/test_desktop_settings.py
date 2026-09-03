"""Native application preferences stored independently of RBSC documents."""

from __future__ import annotations

import json
import os

from rbs.catalog import sample_instance
from rbs.desktop.settings import (
    DesktopSettings,
    DesktopSettingsFile,
    default_settings_path,
    detected_solver_workers,
)
from rbs.models.color_scheme import ColorScheme
from rbs.models.instance import SchedulerInput


def test_default_settings_path_uses_the_application_support_tree(tmp_path) -> None:
    assert default_settings_path(home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "RBS Desktop" / "settings.json"
    )


def test_detected_solver_workers_uses_the_machine_cpu_count(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 12)
    assert detected_solver_workers() == 12

    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert detected_solver_workers() == 1


def test_automatic_worker_count_replaces_saved_machine_specific_value(tmp_path) -> None:
    path = tmp_path / "settings.json"
    raw = sample_instance().model_dump(mode="json")
    raw["solver"]["num_workers"] = 3

    first_machine = DesktopSettingsFile(path, automatic_num_workers=12)
    assert first_machine.capture(SchedulerInput.model_validate(raw))
    assert json.loads(path.read_text(encoding="utf-8"))["solver"]["num_workers"] == 12

    moved_to_smaller_machine = DesktopSettingsFile(path, automatic_num_workers=6)
    restored = moved_to_smaller_machine.apply(sample_instance())
    assert restored.solver.num_workers == 6


def test_first_run_creates_a_private_validated_settings_file(tmp_path) -> None:
    path = tmp_path / "application" / "settings.json"

    settings = DesktopSettingsFile(path)

    assert settings.error is None
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_v1_settings_migrate_and_discard_legacy_color_names() -> None:
    raw = DesktopSettings().model_dump(mode="json")
    raw["schema_version"] = 1
    raw["colors"]["scheme"]["primary"]["name"] = "Legacy Navy"
    raw["colors"]["scheme"]["accents"][0]["name"] = "Legacy Accent"

    migrated = DesktopSettings.model_validate(raw)
    serialized = migrated.model_dump(mode="json")

    assert migrated.schema_version == 2
    assert "name" not in serialized["colors"]["scheme"]["primary"]
    assert "name" not in serialized["colors"]["scheme"]["accents"][0]


def test_settings_capture_and_apply_all_application_preferences(tmp_path) -> None:
    path = tmp_path / "settings.json"
    settings = DesktopSettingsFile(path)
    raw = sample_instance().model_dump(mode="json")
    scheme = ColorScheme().model_dump(mode="json")
    scheme["name"] = "Example University"
    scheme["primary"] = {"name": "Example Blue", "color": "#123A67"}
    raw["color_scheme"] = scheme
    raw["rotations"][0]["color"] = "#123A67"
    raw["clinic_policy"]["sites"][0]["color"] = "#654321"
    raw["solver"]["num_workers"] = 3
    raw["lock_through_today"] = True
    customized = SchedulerInput.model_validate(raw)

    assert settings.capture(customized)
    restored = DesktopSettingsFile(path).apply(sample_instance())

    assert restored.color_scheme.name == "Example University"
    assert restored.rotation(customized.rotations[0].id).color == "#123A67"
    assert restored.clinic_policy.sites[0].color == "#654321"
    assert restored.solver.num_workers == 3
    assert restored.lock_through_today


def test_applying_settings_ignores_document_preferences_and_automatic_locks() -> None:
    raw = sample_instance().model_dump(mode="json")
    raw["color_scheme"]["name"] = "From document"
    raw["solver"]["num_workers"] = 2
    raw["lock_through_today"] = True
    raw["locks"][0]["source"] = "through_today"
    document_instance = SchedulerInput.model_validate(raw)

    applied = DesktopSettings().applied_to(document_instance)

    assert applied.color_scheme.name == "RBS Navy & Gold"
    assert applied.solver.num_workers == 8
    assert not applied.lock_through_today
    assert {lock.source for lock in applied.locks} == {"manual"}


def test_invalid_settings_fall_back_without_overwriting_the_bad_file(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")

    settings = DesktopSettingsFile(path)

    assert settings.error is not None
    assert settings.settings.colors.scheme.name == "RBS Navy & Gold"
    assert path.read_text(encoding="utf-8") == "not json"


def test_failed_atomic_settings_write_preserves_the_previous_file(
    tmp_path, monkeypatch
) -> None:
    import rbs.desktop.settings as desktop_settings

    path = tmp_path / "application" / "settings.json"
    settings = DesktopSettingsFile(path)
    before = path.read_text(encoding="utf-8")
    raw = sample_instance().model_dump(mode="json")
    raw["solver"]["num_workers"] = 3

    def fail_replace(_source, _destination) -> None:
        raise OSError("settings volume full")

    monkeypatch.setattr(desktop_settings.os, "replace", fail_replace)

    assert not settings.capture(SchedulerInput.model_validate(raw))
    assert settings.error == "settings volume full"
    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob(".settings.json.*.tmp")) == []
