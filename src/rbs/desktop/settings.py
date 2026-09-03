"""Per-user application preferences for the native desktop build."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from rbs.models.color_scheme import ColorScheme, normalize_hex_color
from rbs.models.common import StrictModel
from rbs.models.instance import SchedulerInput, SolverConfig

DESKTOP_SETTINGS_SCHEMA_VERSION = 2
MAX_SETTINGS_BYTES = 1024 * 1024
_AUTOMATIC_LOCK_SOURCE = "through_today"


def detected_solver_workers() -> int:
    """Return the solver worker budget available to this desktop process."""
    return max(1, os.cpu_count() or 1)


class DesktopColorSettings(StrictModel):
    """Application-owned palette and entity color assignments."""

    scheme: ColorScheme = Field(default_factory=ColorScheme)
    rotations: dict[str, str] = Field(default_factory=dict)
    clinics: dict[str, str] = Field(default_factory=dict)
    elective: str = "#52606D"

    @field_validator("rotations", "clinics")
    @classmethod
    def normalize_assignments(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_key, raw_color in values.items():
            key = str(raw_key).strip()
            if not key:
                raise ValueError("color assignment IDs cannot be empty")
            normalized[key] = normalize_hex_color(raw_color)
        return dict(sorted(normalized.items()))

    @field_validator("elective")
    @classmethod
    def normalize_elective(cls, value: str) -> str:
        return normalize_hex_color(value)


def _bundled_color_settings() -> DesktopColorSettings:
    """Use the bundled workspace's presentation as the first-run default."""
    from rbs.catalog import current_sample_instance

    instance = current_sample_instance()
    return DesktopColorSettings(
        scheme=instance.color_scheme,
        rotations={rotation.id: rotation.color for rotation in instance.rotations},
        clinics={site.id: site.color for site in instance.clinic_policy.sites},
        elective=instance.electives.color,
    )


class DesktopSettings(StrictModel):
    """Versioned settings persisted outside every workspace document."""

    schema_version: Literal[2] = DESKTOP_SETTINGS_SCHEMA_VERSION
    colors: DesktopColorSettings = Field(default_factory=_bundled_color_settings)
    solver: SolverConfig = Field(default_factory=SolverConfig)
    lock_through_today: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("schema_version") == 1:
            migrated = dict(value)
            migrated["schema_version"] = DESKTOP_SETTINGS_SCHEMA_VERSION
            return migrated
        return value

    def applied_to(self, instance: SchedulerInput) -> SchedulerInput:
        """Overlay these preferences onto direct data loaded from a document."""
        raw = instance.model_dump(mode="json")
        raw["color_scheme"] = self.colors.scheme.model_dump(mode="json")
        raw["solver"] = self.solver.model_dump(mode="json")
        raw["lock_through_today"] = self.lock_through_today
        raw["locks"] = [
            lock
            for lock in raw.get("locks", [])
            if lock.get("source", "manual") != _AUTOMATIC_LOCK_SOURCE
        ]

        for rotation in raw["rotations"]:
            rotation_id = str(rotation["id"])
            rotation["color"] = self.colors.rotations.get(
                rotation_id,
                _fallback_color(rotation_id, self.colors.scheme),
            )
        raw["electives"]["color"] = self.colors.elective
        for clinic in raw["clinic_policy"]["sites"]:
            clinic_id = str(clinic["id"])
            clinic["color"] = self.colors.clinics.get(
                clinic_id,
                _fallback_color(clinic_id, self.colors.scheme),
            )
        return SchedulerInput.from_payload(raw)

    @classmethod
    def captured_from(
        cls,
        instance: SchedulerInput,
        *,
        previous: DesktopSettings | None = None,
    ) -> DesktopSettings:
        """Capture preferences while retaining mappings absent from this document."""
        rotations = dict(previous.colors.rotations) if previous is not None else {}
        rotations.update({rotation.id: rotation.color for rotation in instance.rotations})
        clinics = dict(previous.colors.clinics) if previous is not None else {}
        clinics.update({site.id: site.color for site in instance.clinic_policy.sites})
        return cls(
            colors=DesktopColorSettings(
                scheme=instance.color_scheme,
                rotations=rotations,
                clinics=clinics,
                elective=instance.electives.color,
            ),
            solver=instance.solver,
            lock_through_today=instance.lock_through_today,
        )


def _fallback_color(identifier: str, scheme: ColorScheme) -> str:
    palette = tuple(item.color for item in scheme.selectable_colors)
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    return palette[digest[0] % len(palette)]


def default_settings_path(*, home: Path | None = None) -> Path:
    """Return the conventional per-user settings path."""
    home = Path.home() if home is None else home
    return home / "Library" / "Application Support" / "RBS Desktop" / "settings.json"


class DesktopSettingsFile:
    """Validated, atomic access to one desktop ``settings.json`` file."""

    def __init__(
        self,
        path: str | Path | None,
        *,
        automatic_num_workers: int | None = None,
    ) -> None:
        if automatic_num_workers is not None and automatic_num_workers < 1:
            raise ValueError("automatic_num_workers must be positive")
        self.path = None if path is None else Path(path).expanduser().resolve()
        self.automatic_num_workers = automatic_num_workers
        self._lock = threading.RLock()
        self.settings = self._with_automatic_workers(DesktopSettings())
        self.error: str | None = None
        self._load_or_create()

    def apply(self, instance: SchedulerInput) -> SchedulerInput:
        with self._lock:
            return self.settings.applied_to(instance)

    def capture(self, instance: SchedulerInput) -> bool:
        """Persist the preferences visible in ``instance`` without blocking edits."""
        with self._lock:
            self.settings = self._with_automatic_workers(
                DesktopSettings.captured_from(
                    instance,
                    previous=self.settings,
                )
            )
            return self._save()

    def read_export(self, source: str | Path) -> DesktopSettings:
        """Validate a user-selected settings export without changing application state."""
        path = Path(source).expanduser().resolve()
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ValueError(f"settings file could not be read: {exc}") from exc
        if size > MAX_SETTINGS_BYTES:
            raise ValueError("settings file is larger than the 1 MB limit")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"settings file could not be read: {exc}") from exc
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ValueError("settings file is larger than the 1 MB limit")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("settings file is not UTF-8 JSON") from exc
        try:
            return self._with_automatic_workers(
                DesktopSettings.model_validate_json(raw)
            )
        except Exception as exc:
            raise ValueError(f"settings file could not be read: {exc}") from exc

    def replace(self, settings: DesktopSettings) -> None:
        """Atomically install validated settings as the application's current values."""
        settings = self._with_automatic_workers(settings)
        payload = _settings_payload(settings)
        with self._lock:
            if self.path is not None:
                try:
                    _atomic_write_text(self.path, payload)
                except OSError as exc:
                    self.error = str(exc)
                    raise
            self.settings = settings
            self.error = None

    def export_to(self, destination: str | Path) -> Path:
        """Write a standalone, validated JSON copy of the current settings."""
        path = Path(destination).expanduser().resolve()
        with self._lock:
            _atomic_write_text(path, _settings_payload(self.settings))
        return path

    def _load_or_create(self) -> None:
        if self.path is None:
            return
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self._save()
            return
        except OSError as exc:
            self.error = str(exc)
            return
        if len(raw) > MAX_SETTINGS_BYTES:
            self.error = "settings.json is larger than the 1 MB limit"
            return
        try:
            self.settings = self._with_automatic_workers(
                DesktopSettings.model_validate_json(raw)
            )
        except Exception as exc:
            self.error = f"settings.json could not be read: {exc}"
            return
        self.error = None

    def _with_automatic_workers(self, settings: DesktopSettings) -> DesktopSettings:
        workers = self.automatic_num_workers
        if workers is None or settings.solver.num_workers == workers:
            return settings
        return settings.model_copy(
            update={
                "solver": settings.solver.model_copy(
                    update={"num_workers": workers},
                )
            }
        )

    def _save(self) -> bool:
        if self.path is None:
            self.error = None
            return True
        payload = _settings_payload(self.settings)
        try:
            _atomic_write_text(self.path, payload)
        except OSError as exc:
            self.error = str(exc)
            return False
        self.error = None
        return True


def _settings_payload(settings: DesktopSettings) -> str:
    return (
        json.dumps(
            settings.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _atomic_write_text(destination: Path, payload: str) -> None:
    parent = destination.parent
    created_parent = not parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if created_parent:
        os.chmod(parent, 0o700)

    existing_mode = None
    try:
        existing_mode = destination.stat().st_mode
    except FileNotFoundError:
        pass

    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, existing_mode or (stat.S_IRUSR | stat.S_IWUSR))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "default_settings_path",
    "DesktopColorSettings",
    "DesktopSettings",
    "DesktopSettingsFile",
]
