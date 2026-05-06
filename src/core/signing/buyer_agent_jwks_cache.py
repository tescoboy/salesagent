"""Per-buyer-agent JWKS cache.

Each :class:`Principal` carries ``agent_url`` and (optionally) ``jwks_uri``,
both resolved at admit time from the buyer's brand.json. The verifier picks
``jwks_uri`` when set; otherwise falls back to the same-origin convention
``<agent_url>/.well-known/jwks.json``. We cache
:class:`adcp.signing.CachingJwksResolver` keyed by JWKS URI so concurrent
verifies share one cached JWK set + TTL refresh.

The lib does the heavy lifting (TTL, SSRF validation via IP-pinned transport,
unknown-kid refresh). We just memoize the resolver instance.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from adcp.signing import CachingJwksResolver

if TYPE_CHECKING:
    pass


def _allow_private_destinations() -> bool:
    """Permit private/loopback URLs for dev/test fixtures."""
    if os.getenv("ADCP_AUTH_TEST_MODE", "").lower() in ("1", "true", "yes"):
        return True
    if os.getenv("WEBHOOK_ALLOW_PRIVATE_IPS", "").lower() in ("1", "true", "yes"):
        return True
    return False


def default_jwks_uri_for_agent(agent_url: str) -> str:
    """Same-origin fallback when the buyer's brand.json didn't declare an
    explicit ``jwks_uri``. Mirrors :func:`adcp.signing.brand_jwks._default_jwks_uri`.
    """
    base = agent_url.rstrip("/")
    return f"{base}/.well-known/jwks.json"


class BuyerAgentJwksCache:
    """Process-singleton cache of :class:`CachingJwksResolver` per JWKS URI."""

    def __init__(self) -> None:
        self._resolvers: dict[str, CachingJwksResolver] = {}
        self._lock = asyncio.Lock()

    def resolver_for(self, agent_url: str, jwks_uri: str | None = None) -> CachingJwksResolver:
        """Return the cached resolver for the given JWKS URI.

        ``jwks_uri`` is the column stored on Principal at admit time (resolved
        from brand.json). When ``None`` we fall back to the same-origin
        convention ``<agent_url>/.well-known/jwks.json``.

        Sync (no await) — :class:`CachingJwksResolver` is sync; we don't need
        the lib's async chain walker. The verifier itself is sync, so passing
        a sync resolver into ``VerifyOptions`` is exactly the right shape.
        """
        target = jwks_uri or default_jwks_uri_for_agent(agent_url)
        cached = self._resolvers.get(target)
        if cached is not None:
            return cached
        resolver = CachingJwksResolver(
            jwks_uri=target,
            allow_private=_allow_private_destinations(),
        )
        self._resolvers[target] = resolver
        return resolver

    def clear(self) -> None:
        """Drop all cached resolvers — primarily for tests."""
        self._resolvers.clear()


_singleton: BuyerAgentJwksCache | None = None


def get_buyer_agent_jwks_cache() -> BuyerAgentJwksCache:
    """Return the process-wide cache singleton."""
    global _singleton
    if _singleton is None:
        _singleton = BuyerAgentJwksCache()
    return _singleton
