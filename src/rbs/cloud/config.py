"""Deployment configuration for the hosted build.

Every knob the plan leaves open lives here, and :meth:`CloudConfig.from_env`
refuses to build a configuration that would admit anyone by accident. Failing at
startup is the only safe failure mode for an authorization boundary: a hosted
RBS that starts without knowing who may use it is worse than one that does not
start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

CLOUDFLARE_ACCESS = "cloudflare_access"
IDENTITY_PROVIDERS = (CLOUDFLARE_ACCESS,)

ALLOWLIST = "allowlist"
TRUST_PROXY = "trust_proxy"
AUTHORIZATION_MODES = (ALLOWLIST, TRUST_PROXY)

DEFAULT_RETENTION_DAYS = 90
DEFAULT_RETENTION_WARNING_DAYS = 7
DEFAULT_DESK_CAP = 10
DEFAULT_SOLVE_WORKERS = 4
DEFAULT_SOLVE_CEILING_SECONDS = 120.0
DEFAULT_UPLOAD_MAX_BYTES = 32 * 1024 * 1024


class ConfigurationError(RuntimeError):
    """Raised when the deployment is not safe to start."""


@dataclass(frozen=True, slots=True)
class CloudConfig:
    cf_team_domain: str
    cf_audience: str
    storage_secret: str
    identity_provider: str = CLOUDFLARE_ACCESS
    authorization_mode: str = ALLOWLIST
    bootstrap_subjects: tuple[str, ...] = ()
    control_db: Path = field(default_factory=lambda: Path("control.sqlite"))
    data_dir: Path = field(default_factory=lambda: Path("data"))
    retention: timedelta = timedelta(days=DEFAULT_RETENTION_DAYS)
    retention_warning: timedelta = timedelta(days=DEFAULT_RETENTION_WARNING_DAYS)
    desk_cap: int = DEFAULT_DESK_CAP
    solve_pool_size: int = 0  # 0 selects a size from the host's core count
    solve_workers: int = DEFAULT_SOLVE_WORKERS
    solve_ceiling_seconds: float = DEFAULT_SOLVE_CEILING_SECONDS
    upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES
    host: str = "127.0.0.1"
    port: int = 8080

    def __post_init__(self) -> None:
        if self.identity_provider not in IDENTITY_PROVIDERS:
            raise ConfigurationError(
                f"unknown identity provider {self.identity_provider!r}; "
                f"expected one of {', '.join(IDENTITY_PROVIDERS)}"
            )
        if self.authorization_mode not in AUTHORIZATION_MODES:
            raise ConfigurationError(
                f"unknown authorization mode {self.authorization_mode!r}; "
                f"expected one of {', '.join(AUTHORIZATION_MODES)}"
            )
        if self.identity_provider == CLOUDFLARE_ACCESS:
            if not self.cf_team_domain:
                raise ConfigurationError("cloudflare_access requires a team domain")
            if not self.cf_audience:
                raise ConfigurationError(
                    "cloudflare_access requires an application audience tag; without "
                    "it a token minted for any other application in the same team "
                    "would validate here"
                )
        if not self.storage_secret:
            raise ConfigurationError("a storage secret is required to sign session cookies")
        if self.authorization_mode == ALLOWLIST and not self.bootstrap_subjects:
            # An empty allowlist with no bootstrap admits nobody, and there is no
            # in-app way to add the first user. Refusing here turns a silent
            # lockout into a startup error that names the fix.
            raise ConfigurationError(
                "allowlist mode needs at least one bootstrap subject, or nobody "
                "can ever be admitted; set RBS_BOOTSTRAP_SUBJECTS or use "
                "RBS_AUTHORIZATION_MODE=trust_proxy"
            )
        if self.retention <= timedelta(0):
            raise ConfigurationError("retention must be positive")
        if self.desk_cap < 1:
            raise ConfigurationError("desk cap must be at least 1")
        if self.solve_ceiling_seconds <= 0:
            raise ConfigurationError("solve ceiling must be positive")

    @property
    def issuer(self) -> str:
        return f"https://{self.cf_team_domain}"

    @property
    def jwks_url(self) -> str:
        return f"https://{self.cf_team_domain}/cdn-cgi/access/certs"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CloudConfig:
        source = os.environ if env is None else env

        def text(name: str, default: str = "") -> str:
            return str(source.get(name, default)).strip()

        def number(name: str, default: int) -> int:
            raw = text(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc

        def decimal(name: str, default: float) -> float:
            raw = text(name)
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc

        subjects = tuple(
            item.strip() for item in text("RBS_BOOTSTRAP_SUBJECTS").split(",") if item.strip()
        )
        return cls(
            cf_team_domain=text("RBS_CF_TEAM_DOMAIN"),
            cf_audience=text("RBS_CF_AUDIENCE"),
            storage_secret=text("RBS_STORAGE_SECRET"),
            identity_provider=text("RBS_IDENTITY_PROVIDER", CLOUDFLARE_ACCESS),
            authorization_mode=text("RBS_AUTHORIZATION_MODE", ALLOWLIST),
            bootstrap_subjects=subjects,
            control_db=Path(text("RBS_CONTROL_DB", "control.sqlite")),
            data_dir=Path(text("RBS_DATA_DIR", "data")),
            retention=timedelta(days=number("RBS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)),
            retention_warning=timedelta(
                days=number("RBS_RETENTION_WARNING_DAYS", DEFAULT_RETENTION_WARNING_DAYS)
            ),
            desk_cap=number("RBS_DESK_CAP", DEFAULT_DESK_CAP),
            solve_pool_size=number("RBS_SOLVE_POOL_SIZE", 0),
            solve_workers=number("RBS_SOLVE_WORKERS", DEFAULT_SOLVE_WORKERS),
            solve_ceiling_seconds=decimal(
                "RBS_SOLVE_CEILING_SECONDS", DEFAULT_SOLVE_CEILING_SECONDS
            ),
            upload_max_bytes=number("RBS_UPLOAD_MAX_BYTES", DEFAULT_UPLOAD_MAX_BYTES),
            host=text("RBS_HOST", "127.0.0.1"),
            port=number("RBS_PORT", 8080),
        )

    def resolved_solve_pool_size(self) -> int:
        """How many solves may run at once without oversubscribing the box."""
        if self.solve_pool_size > 0:
            return self.solve_pool_size
        cores = os.cpu_count() or 1
        return max(1, cores // max(1, self.solve_workers))
