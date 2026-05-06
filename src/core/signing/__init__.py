"""Salesagent-side signing infrastructure (per-buyer-agent trust model).

Each Principal optionally carries an ``agent_url``. When set, the verifier
fetches JWKS from ``<agent_url>/.well-known/jwks.json`` and verifies inbound
signatures against it. The salesagent trusts the agent for its operator
identity — no brand.json chain walk. Operators and brands are orthogonal
dimensions of Accounts, not auth subjects.

This module owns the local glue around the ``adcp.signing`` library:

* :class:`BuyerAgentJwksCache` — process-singleton cache of
  :class:`adcp.signing.CachingJwksResolver` keyed on ``agent_url``.
* :class:`SigningVerifyMiddleware` — Starlette ASGI middleware that
  enforces per-tenant signing policy + verifies signatures.
* ``replay_store`` — :class:`adcp.signing.PgReplayStore` bootstrap with
  ``pg_cron`` auto-detection + in-process timer fallback.
* :class:`VerifiedRequestState` + contextvar bridge for downstream handlers.
"""

from __future__ import annotations

from src.core.signing.buyer_agent_jwks_cache import (
    BuyerAgentJwksCache,
    default_jwks_uri_for_agent,
    get_buyer_agent_jwks_cache,
)
from src.core.signing.middleware import SigningVerifyMiddleware
from src.core.signing.replay_store import (
    bootstrap_replay_store,
    get_replay_store,
)
from src.core.signing.verified_state import (
    VerifiedRequestState,
    clear_verified_state,
    get_verified_state,
    set_verified_state,
)

__all__ = [
    "BuyerAgentJwksCache",
    "SigningVerifyMiddleware",
    "VerifiedRequestState",
    "bootstrap_replay_store",
    "clear_verified_state",
    "get_buyer_agent_jwks_cache",
    "get_replay_store",
    "default_jwks_uri_for_agent",
    "get_verified_state",
    "set_verified_state",
]
