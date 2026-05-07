"""Per-buyer-agent brand.json resolver cache.

Each :class:`Principal` carries a ``brand_domain`` (operator-typed buyer
domain — the trust anchor) that points at
``https://<brand_domain>/.well-known/brand.json``. The verifier looks up
JWKS by walking that brand.json via :class:`adcp.signing.BrandJsonJwksResolver`,
which handles:

* Cooldown-respecting brand.json re-walks (default 1h, honors counterparty
  ``Cache-Control``)
* Unknown-kid cascade: when the verifier asks for a kid the resolver hasn't
  seen, the resolver re-fetches brand.json + JWKS rather than returning
  ``None`` and forcing operator intervention
* IP-pinned transport (DNS-rebinding-safe)
* Same-origin guard on the implicit ``jwks_uri`` fallback

We cache one resolver instance per ``(tenant_id, principal_id)`` so concurrent
verifies on the same principal share the cooldown window + JWK set without
re-walking brand.json from scratch.

This replaces the prior model where we stored ``jwks_uri`` as a column and
constructed a :class:`CachingJwksResolver` directly — that bypassed the
library's brand.json-walk semantics, so a buyer rotating ``jwks_uri`` in
their brand.json would never propagate without manual operator re-resolve.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from adcp.signing.brand_jwks import BrandJsonJwksResolver

if TYPE_CHECKING:
    pass


def _allow_private_destinations() -> bool:
    """Permit private/loopback URLs for dev/test fixtures."""
    if os.getenv("ADCP_AUTH_TEST_MODE", "").lower() in ("1", "true", "yes"):
        return True
    if os.getenv("WEBHOOK_ALLOW_PRIVATE_IPS", "").lower() in ("1", "true", "yes"):
        return True
    return False


def brand_json_url_for(brand_domain: str) -> str:
    """Convention: each buyer publishes brand.json at the well-known path on
    their typed domain. This is the trust-root URL the resolver walks."""
    return f"https://{brand_domain.strip('/')}/.well-known/brand.json"


class BuyerAgentJwksCache:
    """Process-singleton cache of :class:`BrandJsonJwksResolver` per
    ``(tenant_id, principal_id)``.

    Library resolvers are async + designed to be long-lived: their internal
    snapshot/cooldown/unknown-kid-cascade only works correctly when the same
    instance is reused across requests. Memoize per-principal so we don't
    construct one per verify.
    """

    def __init__(self) -> None:
        self._resolvers: dict[tuple[str, str], BrandJsonJwksResolver] = {}
        self._lock = asyncio.Lock()

    def resolver_for(self, tenant_id: str, principal_id: str, brand_domain: str) -> BrandJsonJwksResolver:
        """Return the cached resolver. Lazy-construct on first use.

        ``brand_domain`` is the operator-typed buyer domain. The library's
        :class:`BrandJsonJwksResolver` walks ``https://<brand_domain>/.well-known/brand.json``
        and selects the buyer-protocol agent (``agent_type="buying"``).
        """
        key = (tenant_id, principal_id)
        cached = self._resolvers.get(key)
        if cached is not None:
            return cached
        resolver = BrandJsonJwksResolver(
            brand_json_url=brand_json_url_for(brand_domain),
            agent_type="buying",
            allow_private_destinations=_allow_private_destinations(),
        )
        self._resolvers[key] = resolver
        return resolver

    def invalidate(self, tenant_id: str, principal_id: str) -> None:
        """Drop the cached resolver — call when the operator changes
        ``brand_domain`` so the next verify picks up the new trust root."""
        self._resolvers.pop((tenant_id, principal_id), None)

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
