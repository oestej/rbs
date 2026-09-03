"""Authentication adapters: read a principal someone else already vouched for.

RBS never authenticates. There is no login route, no credential handling, no
token exchange and no password store anywhere in this package. An adapter's
entire job is to verify an assertion the deployment's proxy already made and
turn it into a :class:`~rbs.ui.host.Principal`.

That constraint is why there will never be a direct-OIDC adapter here: owning
callback routes and refresh tokens *is* managing authentication. Every adapter
that gets added later - oauth2-proxy, Authelia, Tailscale Serve, Google IAP -
has the same shape as the Cloudflare one below.
"""

from __future__ import annotations

import threading
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from rbs.cloud.config import CLOUDFLARE_ACCESS, CloudConfig, ConfigurationError
from rbs.ui.host import Principal

ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_COOKIE = "CF_Authorization"
CLOUDFLARE_PROVIDER = "cloudflare_access"


class IdentityAdapter(Protocol):
    """Extract an already-authenticated principal. Never authenticates."""

    def resolve(self, request) -> Principal | None: ...


class CloudflareAccessAdapter:
    """Validate a Cloudflare Access JWT against the team's signing keys.

    The signature check is the whole security boundary. ``CF-Access-Authenticated-
    User-Email`` and friends are plain headers that anything able to reach the
    origin can set, so they are never read here - the origin must additionally be
    locked down (a ``cloudflared`` tunnel with no public ingress) or a direct
    caller could simply present its own token-free request.
    """

    def __init__(
        self,
        config: CloudConfig,
        *,
        jwk_client: Any | None = None,
        leeway: float = 30.0,
    ) -> None:
        self._issuer = config.issuer
        self._audience = config.cf_audience
        self._leeway = leeway
        self._lock = threading.Lock()
        self._jwk_client = jwk_client or PyJWKClient(
            config.jwks_url,
            cache_keys=True,
            lifespan=600,
        )

    def resolve(self, request) -> Principal | None:
        token = _access_token(request)
        if not token:
            return None
        claims = self._verified_claims(token)
        if claims is None:
            return None
        subject = str(claims.get("sub") or "").strip()
        if not subject:
            # Access service tokens authenticate a machine and carry an empty
            # ``sub``. They are not people, and a desk belongs to a person.
            return None
        email = claims.get("email")
        return Principal(
            subject=subject,
            display=str(email) if email else None,
            provider=CLOUDFLARE_PROVIDER,
        )

    def _verified_claims(self, token: str) -> dict[str, Any] | None:
        try:
            key = self._signing_key(token)
        except Exception:
            return None
        try:
            return jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.InvalidTokenError:
            return None

    def _signing_key(self, token: str):
        # PyJWKClient is not documented as thread-safe and NiceGUI resolves
        # identity from multiple request tasks.
        with self._lock:
            return self._jwk_client.get_signing_key_from_jwt(token).key


def _access_token(request) -> str | None:
    """Pull the Access assertion from the header, falling back to its cookie.

    The header is what the proxy sets on forwarded requests; the cookie covers
    paths where it does not survive, notably some websocket upgrades.
    """
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is not None:
        token = headers.get(ACCESS_HEADER)
        if token:
            return str(token).strip()
    cookies = getattr(request, "cookies", None)
    if cookies is not None:
        token = cookies.get(ACCESS_COOKIE)
        if token:
            return str(token).strip()
    return None


def build_identity_adapter(config: CloudConfig, **kwargs: Any) -> IdentityAdapter:
    if config.identity_provider == CLOUDFLARE_ACCESS:
        return CloudflareAccessAdapter(config, **kwargs)
    raise ConfigurationError(f"unsupported identity provider {config.identity_provider!r}")
