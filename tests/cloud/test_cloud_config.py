"""Startup refuses any configuration that would admit someone by accident.

A hosted RBS that starts without knowing who may use it is worse than one that
does not start, so each of these is a deliberate crash rather than a warning.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from rbs.cloud.config import ALLOWLIST, TRUST_PROXY, CloudConfig, ConfigurationError

COMPLETE = {
    "RBS_CF_TEAM_DOMAIN": "acme.cloudflareaccess.com",
    "RBS_CF_AUDIENCE": "aud-for-this-app",
    "RBS_STORAGE_SECRET": "signing-secret",
    "RBS_BOOTSTRAP_SUBJECTS": "subject-a, subject-b",
}


def test_a_complete_environment_builds() -> None:
    config = CloudConfig.from_env(COMPLETE)

    assert config.issuer == "https://acme.cloudflareaccess.com"
    assert config.jwks_url == "https://acme.cloudflareaccess.com/cdn-cgi/access/certs"
    assert config.bootstrap_subjects == ("subject-a", "subject-b")
    assert config.authorization_mode == ALLOWLIST
    assert config.retention == timedelta(days=90)


@pytest.mark.parametrize(
    ("label", "missing", "match"),
    [
        ("audience", "RBS_CF_AUDIENCE", "audience"),
        ("team domain", "RBS_CF_TEAM_DOMAIN", "team domain"),
        ("storage secret", "RBS_STORAGE_SECRET", "storage secret"),
        ("bootstrap subjects", "RBS_BOOTSTRAP_SUBJECTS", "bootstrap subject"),
    ],
)
def test_incomplete_configuration_refuses_to_start(label, missing, match) -> None:
    env = {key: value for key, value in COMPLETE.items() if key != missing}

    with pytest.raises(ConfigurationError, match=match):
        CloudConfig.from_env(env)


def test_trust_proxy_mode_does_not_need_a_bootstrap_list() -> None:
    """Deployments that let the proxy policy decide have nobody to bootstrap."""
    env = {key: value for key, value in COMPLETE.items() if key != "RBS_BOOTSTRAP_SUBJECTS"}
    env["RBS_AUTHORIZATION_MODE"] = TRUST_PROXY

    assert CloudConfig.from_env(env).authorization_mode == TRUST_PROXY


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("RBS_IDENTITY_PROVIDER", "homegrown_oidc"),
        ("RBS_AUTHORIZATION_MODE", "everyone"),
    ],
)
def test_unknown_modes_are_refused(setting, value) -> None:
    with pytest.raises(ConfigurationError, match="unknown"):
        CloudConfig.from_env({**COMPLETE, setting: value})


@pytest.mark.parametrize(
    ("setting", "value", "match"),
    [
        ("RBS_RETENTION_DAYS", "0", "retention must be positive"),
        ("RBS_DESK_CAP", "0", "desk cap"),
        ("RBS_SOLVE_CEILING_SECONDS", "0", "solve ceiling"),
    ],
)
def test_nonsensical_limits_are_refused(setting, value, match) -> None:
    with pytest.raises(ConfigurationError, match=match):
        CloudConfig.from_env({**COMPLETE, setting: value})


def test_non_numeric_settings_name_the_offending_variable() -> None:
    with pytest.raises(ConfigurationError, match="RBS_DESK_CAP"):
        CloudConfig.from_env({**COMPLETE, "RBS_DESK_CAP": "ten"})


def test_solve_pool_size_falls_back_to_the_core_count() -> None:
    explicit = CloudConfig.from_env({**COMPLETE, "RBS_SOLVE_POOL_SIZE": "3"})
    assert explicit.resolved_solve_pool_size() == 3

    derived = CloudConfig.from_env(COMPLETE)
    assert derived.resolved_solve_pool_size() >= 1
