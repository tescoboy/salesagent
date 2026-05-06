"""Map ``Authorization: Bearer <token>`` to ``x-adcp-auth: <token>``.

The ``adcp.server.serve()`` SDK passes one shared :class:`BearerTokenAuth`
config to both the MCP and A2A legs. We configure the SDK with
``header_name="x-adcp-auth"`` (the AdCP convention) so MCP buyers and
existing tests work as they always have. But the official ``a2a-sdk``
client sends ``Authorization: Bearer <token>`` per RFC 6750 — that's the
HTTP standard and what ``a2a/client/auth/interceptor.py`` emits for HTTP
Bearer security schemes.

Without this shim, A2A traffic from real buyers (and from the e2e suite)
gets a 401 because ``A2ABearerAuthMiddleware`` looks at ``x-adcp-auth``
and finds nothing.

This middleware sits *before* the SDK auth middleware in the ASGI stack.
On HTTP requests it inspects the inbound headers:

* If ``x-adcp-auth`` is already present → pass through untouched.
* Else if ``Authorization`` is ``Bearer <token>`` → inject
  ``x-adcp-auth: <token>`` into the scope's header list.
* Else → pass through; the auth middleware will reject as before.

The injection builds a new tuple list and a new scope dict; the
caller's scope is not mutated. Non-HTTP scopes (lifespan, websocket)
pass through with the original scope object.

Removable when the ``adcp`` SDK supports per-leg ``BearerTokenAuth``
configuration (tracked at https://github.com/bokelley/salesagent/issues/57).
At that point switch the A2A leg to ``header_name="Authorization"`` +
``bearer_prefix_required=True`` and delete this middleware + its tests.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_X_ADCP_AUTH = b"x-adcp-auth"
_AUTHORIZATION = b"authorization"
_BEARER_PREFIX = b"bearer "


class BearerToAdcpAuthMiddleware:
    """ASGI middleware: copy ``Authorization: Bearer X`` to ``x-adcp-auth: X``.

    No-op when ``x-adcp-auth`` already present — operator-supplied
    headers always win.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Iterate the original scope headers without copying — only
        # allocate when we actually need to inject. Operator-supplied
        # ``x-adcp-auth`` always wins (precedence: explicit beats
        # translated), so a buyer that sends BOTH headers gets the
        # canonical one through untouched and the bearer header is
        # ignored. Header order on the wire doesn't matter; the
        # ``has_adcp_auth`` flag short-circuits injection at the gate
        # below regardless of which scan position found the canonical
        # header first.
        raw_headers: tuple[tuple[bytes, bytes], ...] = tuple(scope.get("headers") or ())
        has_adcp_auth = False
        bearer_token: bytes | None = None
        adcp_auth_value: bytes | None = None

        for name, value in raw_headers:
            if name == _X_ADCP_AUTH:
                has_adcp_auth = True
                adcp_auth_value = value
                # Don't break — keep scanning for Authorization so we
                # can log a dual-credential mismatch (useful audit
                # signal for credential-confusion / proxy misconfig).
            elif name == _AUTHORIZATION and bearer_token is None:
                # Authorization values are ASCII per RFC 7230; scheme
                # match is case-insensitive per RFC 6750 §2.1.
                if value.lower().startswith(_BEARER_PREFIX):
                    bearer_token = value[len(_BEARER_PREFIX) :].strip()

        if has_adcp_auth:
            if bearer_token and bearer_token != adcp_auth_value:
                # Different tokens in both headers — operator's wins,
                # but this is suspicious enough to surface for audit.
                # Don't log token values.
                logger.warning(
                    "bearer_to_adcp_auth: request has both x-adcp-auth and "
                    "Authorization: Bearer with different tokens; "
                    "x-adcp-auth wins, Authorization ignored. Path=%s",
                    scope.get("path", ""),
                )
            await self._app(scope, receive, send)
            return

        if bearer_token:
            # Inject x-adcp-auth on a fresh header list + scope dict so
            # the caller's scope is not mutated.
            new_headers: list[tuple[bytes, bytes]] = [*raw_headers, (_X_ADCP_AUTH, bearer_token)]
            new_scope: dict[str, Any] = {**scope, "headers": new_headers}
            logger.debug(
                "bearer_to_adcp_auth: translated Authorization: Bearer → x-adcp-auth (path=%s)",
                scope.get("path", ""),
            )
            await self._app(new_scope, receive, send)
            return

        await self._app(scope, receive, send)
