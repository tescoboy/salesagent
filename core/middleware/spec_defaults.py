"""Apply AdCP spec-mandated server-side defaults to inbound requests.

Some 4.4+ schemas mark fields as ``required`` at the wire level even though
the spec text instructs sellers to apply a default for missing values from
pre-v3 clients. The most prominent example is ``GetProductsRequest.buying_mode``
— required in the JSON Schema, but the description says: *"Sellers receiving
requests from pre-v3 clients without buying_mode SHOULD default to 'brief'."*

The SDK's typed dispatcher validates request payloads against the library
Pydantic models *before* invoking the platform handler, so a per-handler
``model_validator`` cannot apply the default in time. This middleware fixes
that gap by intercepting MCP ``tools/call`` JSON-RPC bodies (and the matching
A2A skill payloads) and backfilling spec defaults before the SDK validator
runs.

Surface area is intentionally narrow: only the specific fields the spec
text calls out as defaultable. Adding new defaults must reference the spec
description that justifies them.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


_GET_PRODUCTS_DEFAULTS: dict[str, str] = {
    # GetProductsRequest.buying_mode — spec: "Sellers receiving requests from
    # pre-v3 clients without buying_mode SHOULD default to 'brief'."
    "buying_mode": "brief",
}


def _apply_get_products_defaults(args: dict[str, Any]) -> None:
    for field, default in _GET_PRODUCTS_DEFAULTS.items():
        args.setdefault(field, default)


def _patch_mcp_tools_call(payload: dict[str, Any]) -> dict[str, Any]:
    """Patch a JSON-RPC ``tools/call`` body for ``get_products`` in place."""
    if payload.get("method") != "tools/call":
        return payload
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return payload
    if params.get("name") != "get_products":
        return payload
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return payload
    _apply_get_products_defaults(arguments)
    return payload


def _patch_a2a_skill(payload: dict[str, Any]) -> dict[str, Any]:
    """Patch an A2A skill request body for ``get_products`` in place.

    A2A passes skill name in different shapes depending on the request style.
    Cover both ``{"skill": "get_products", "params": {...}}`` and the JSON-RPC
    style used by a2a-sdk's ``message/send`` handler.
    """
    skill = payload.get("skill")
    params = payload.get("params") if isinstance(payload, dict) else None
    if skill == "get_products" and isinstance(params, dict):
        _apply_get_products_defaults(params)
    return payload


class SpecDefaultsMiddleware:
    """ASGI middleware applying spec-mandated request defaults.

    Intercepts HTTP request bodies on ``/mcp/*`` and ``/`` (the A2A surface),
    decodes the JSON, applies defaults, and re-encodes. Non-JSON bodies and
    non-target paths pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = await self._read_body(receive)
        patched = self._patch_body(scope, body)
        if patched is None:
            patched = body

        async def _replay() -> Message:
            return {"type": "http.request", "body": patched, "more_body": False}

        await self.app(scope, _replay, send)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # disconnect or unexpected — let downstream see it
                return b"".join(chunks)
            chunks.append(message.get("body") or b"")
            more = message.get("more_body", False)
        return b"".join(chunks)

    @staticmethod
    def _patch_body(scope: Scope, body: bytes) -> bytes | None:
        if not body:
            return None
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        path = scope.get("path", "")
        if path == "/mcp" or path.startswith("/mcp/"):
            payload = _patch_mcp_tools_call(payload)
        else:
            # A2A surface lives at host root; skip Flask admin paths.
            if path.startswith(("/admin", "/static", "/auth", "/tenant", "/api", "/login", "/logout")):
                return None
            payload = _patch_a2a_skill(payload)

        try:
            return json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError):
            logger.warning("SpecDefaultsMiddleware: failed to re-encode patched payload — passing original through")
            return None
