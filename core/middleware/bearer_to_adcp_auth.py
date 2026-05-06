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

The injection mutates ``scope["headers"]`` (a new tuple list), not the
original list. Non-HTTP scopes (lifespan, websocket) pass through.
"""

from __future__ import annotations

from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

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

        headers: list[tuple[bytes, bytes]] = list(scope.get("headers") or [])
        has_adcp_auth = False
        bearer_token: bytes | None = None

        for name, value in headers:
            if name == _X_ADCP_AUTH:
                has_adcp_auth = True
                break  # already authenticated via canonical header
            if name == _AUTHORIZATION and bearer_token is None:
                # Authorization values are ASCII per RFC 7230; case-insensitive
                # scheme match per RFC 6750 §2.1.
                if value[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
                    bearer_token = value[len(_BEARER_PREFIX) :].strip()

        if not has_adcp_auth and bearer_token:
            # Mutate scope headers to include the injected x-adcp-auth.
            # ASGI spec allows operator middleware to add headers as long
            # as the list is replaced, not mutated in place (the original
            # may be referenced elsewhere).
            new_headers: list[tuple[bytes, bytes]] = [*headers, (_X_ADCP_AUTH, bearer_token)]
            new_scope: dict[str, Any] = {**scope, "headers": new_headers}
            await self._app(new_scope, receive, send)
            return

        await self._app(scope, receive, send)
