"""Unit tests for ``BearerToAdcpAuthMiddleware``.

The middleware translates RFC 6750 ``Authorization: Bearer <token>`` to
the ``x-adcp-auth: <token>`` header the SDK auth chain expects, so the
A2A surface (where a2a-sdk clients emit ``Authorization: Bearer``) and
the MCP surface (where adopters use ``x-adcp-auth``) can share one
``BearerTokenAuth`` config.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.middleware.bearer_to_adcp_auth import BearerToAdcpAuthMiddleware


class _CapturingApp:
    """Minimal ASGI app that records the scope it was called with."""

    def __init__(self) -> None:
        self.captured_scope: dict[str, Any] | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.captured_scope = scope


def _http_scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": "/", "headers": headers}


@pytest.mark.asyncio
async def test_authorization_bearer_is_mapped_to_x_adcp_auth():
    """``Authorization: Bearer X`` injects ``x-adcp-auth: X`` when missing."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"authorization", b"Bearer ci-test-token")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert headers.get(b"x-adcp-auth") == b"ci-test-token"
    # Original Authorization header preserved for downstream consumers.
    assert headers.get(b"authorization") == b"Bearer ci-test-token"


@pytest.mark.asyncio
async def test_existing_x_adcp_auth_wins_over_authorization():
    """When both headers present, ``x-adcp-auth`` is kept untouched —
    operator-supplied headers are authoritative."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope(
        [
            (b"x-adcp-auth", b"original-token"),
            (b"authorization", b"Bearer different-token"),
        ]
    )

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    # Find the ONE x-adcp-auth header (no duplicate injection).
    adcp_auth_values = [v for n, v in inner.captured_scope["headers"] if n == b"x-adcp-auth"]
    assert adcp_auth_values == [b"original-token"]


@pytest.mark.asyncio
async def test_no_authorization_header_passes_through():
    """No Authorization header → no injection, request reaches inner unchanged."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"content-type", b"application/json")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert b"x-adcp-auth" not in headers


@pytest.mark.asyncio
async def test_bearer_scheme_is_case_insensitive():
    """RFC 6750 §2.1 — scheme matching is case-insensitive."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"authorization", b"BEARER ci-test-token")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert headers.get(b"x-adcp-auth") == b"ci-test-token"


@pytest.mark.asyncio
async def test_non_bearer_scheme_is_ignored():
    """``Basic`` / ``Digest`` / etc. don't map to bearer tokens."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"authorization", b"Basic dXNlcjpwYXNz")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert b"x-adcp-auth" not in headers


@pytest.mark.asyncio
async def test_lifespan_scope_passes_through():
    """Non-HTTP scopes (lifespan, websocket) bypass the middleware."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = {"type": "lifespan"}

    await mw(scope, lambda: None, lambda msg: None)

    # Inner was called with the original scope reference (no copy).
    assert inner.captured_scope is scope


@pytest.mark.asyncio
async def test_websocket_scope_passes_through():
    """Websocket scopes bypass auth-header translation."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = {"type": "websocket", "headers": [(b"authorization", b"Bearer x")]}

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is scope
    # No mutation — the scope dict is the same object.
    assert inner.captured_scope["headers"] == [(b"authorization", b"Bearer x")]


@pytest.mark.asyncio
async def test_bearer_value_is_stripped_of_surrounding_whitespace():
    """Tolerate ``Bearer   <token>  `` shapes some HTTP libraries emit."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"authorization", b"Bearer   ci-test-token  ")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert headers.get(b"x-adcp-auth") == b"ci-test-token"


@pytest.mark.asyncio
async def test_empty_bearer_token_is_not_injected():
    """``Authorization: Bearer `` (empty) must not inject empty token —
    the auth middleware would treat empty == missing differently."""
    inner = _CapturingApp()
    mw = BearerToAdcpAuthMiddleware(inner)
    scope = _http_scope([(b"authorization", b"Bearer ")])

    await mw(scope, lambda: None, lambda msg: None)

    assert inner.captured_scope is not None
    headers = dict(inner.captured_scope["headers"])
    assert b"x-adcp-auth" not in headers
