"""The Cloudflare Access adapter, exercised against real RS256 signatures.

The signature check is the entire security boundary, so these tests mint genuine
tokens rather than stubbing the decode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from rbs.cloud.config import CloudConfig, ConfigurationError
from rbs.cloud.identity import (
    ACCESS_COOKIE,
    ACCESS_HEADER,
    CloudflareAccessAdapter,
    build_identity_adapter,
)

TEAM = "acme.cloudflareaccess.com"
AUDIENCE = "aud-for-this-app"


@pytest.fixture(scope="module")
def keys() -> tuple:
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return signing, other


@pytest.fixture
def config() -> CloudConfig:
    return CloudConfig(
        cf_team_domain=TEAM,
        cf_audience=AUDIENCE,
        storage_secret="secret",
        bootstrap_subjects=("bootstrap-subject",),
    )


class _Request:
    def __init__(self, headers: dict | None = None, cookies: dict | None = None) -> None:
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {})


class _StubJWKClient:
    """Stands in for the network fetch of the team's public keys."""

    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: ARG002 - one key in tests
        return type("Key", (), {"key": self._public_key})()


def _adapter(config: CloudConfig, public_key) -> CloudflareAccessAdapter:
    return CloudflareAccessAdapter(config, jwk_client=_StubJWKClient(public_key))


def _token(private_key, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": "subject-123",
        "aud": AUDIENCE,
        "iss": f"https://{TEAM}",
        "iat": now - timedelta(minutes=1),
        "exp": now + timedelta(hours=1),
        "email": "coordinator@example.org",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_valid_token_resolves_to_the_subject_claim(config, keys) -> None:
    signing, _ = keys
    adapter = _adapter(config, signing.public_key())

    principal = adapter.resolve(_Request({ACCESS_HEADER: _token(signing)}))

    assert principal is not None
    assert principal.subject == "subject-123"
    assert principal.display == "coordinator@example.org"
    assert principal.provider == "cloudflare_access"


def test_token_is_read_from_the_cookie_when_the_header_is_absent(config, keys) -> None:
    signing, _ = keys
    adapter = _adapter(config, signing.public_key())

    principal = adapter.resolve(_Request(cookies={ACCESS_COOKIE: _token(signing)}))

    assert principal is not None and principal.subject == "subject-123"


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("wrong audience", {"aud": "some-other-application"}),
        ("wrong issuer", {"iss": "https://attacker.example"}),
        ("expired", {"exp": datetime.now(UTC) - timedelta(hours=1)}),
        ("missing subject", {"sub": ""}),
    ],
)
def test_unacceptable_claims_are_rejected(config, keys, label, overrides) -> None:
    signing, _ = keys
    adapter = _adapter(config, signing.public_key())

    assert adapter.resolve(_Request({ACCESS_HEADER: _token(signing, **overrides)})) is None, label


def test_token_signed_by_another_key_is_rejected(config, keys) -> None:
    signing, other = keys
    adapter = _adapter(config, signing.public_key())

    assert adapter.resolve(_Request({ACCESS_HEADER: _token(other)})) is None


def test_the_email_header_alone_is_not_an_identity(config, keys) -> None:
    """The header a proxy sets is forgeable; only the signed token counts."""
    signing, _ = keys
    adapter = _adapter(config, signing.public_key())

    request = _Request({"CF-Access-Authenticated-User-Email": "coordinator@example.org"})

    assert adapter.resolve(request) is None


def test_absent_request_or_token_resolves_to_nobody(config, keys) -> None:
    signing, _ = keys
    adapter = _adapter(config, signing.public_key())

    assert adapter.resolve(None) is None
    assert adapter.resolve(_Request()) is None


def test_build_rejects_an_unsupported_provider(config, keys) -> None:
    """The factory is the last gate; a provider it cannot serve must not start."""
    signing, _ = keys
    unsupported = object.__new__(CloudConfig)
    object.__setattr__(unsupported, "identity_provider", "some_future_proxy")

    with pytest.raises(ConfigurationError, match="unsupported identity provider"):
        build_identity_adapter(unsupported, jwk_client=_StubJWKClient(signing.public_key()))
