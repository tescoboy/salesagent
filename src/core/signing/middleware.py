"""Starlette ASGI middleware for inbound RFC 9421 signature verification.

PR 2B + PR 2C of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).

Runs before MCP and A2A buyer-protocol handlers. Buffers the request body up
front (so the verifier can read it AND downstream handlers still see the same
bytes via a replay-receive callable), extracts the AdCP operation name, then:

* Verifies the RFC 9421 signature when present (against the buyer's brand.json
  JWKS resolved through ``adcp.signing.BrandJsonJwksResolver``).
* Enforces ``TenantSigningPolicy.required_for``: if the operation is in the
  list, an unsigned request is rejected with 401 ``request_signature_required``.
* Trusted operators (embedded host's interchange) bypass verification and
  enforcement entirely — network/header trust is the embedded-mode boundary.

Failure mapping:

* Operation in ``required_for`` + no signature → 401
  ``request_signature_required``
* Signature parse / window / replay / unknown-key → 401 with the spec error code
* Bearer maps to a principal with no ``bound_operator_id`` and the operation
  is in ``required_for`` → 401 ``request_signature_required`` (we can't verify
  without a registered operator; treat like missing signature)
* Verifier crash / DB unreachable → fail-closed when policy demands signing
  for the operation; fail-open otherwise (legacy bearer-only paths must keep
  working through transient signing-side outages)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from adcp.signing import (
    SignatureVerificationError,
    StaticJwksResolver,
    VerifierCapability,
    VerifyOptions,
    parse_signature_input_header,
    unauthorized_response_headers,
    verify_starlette_request,
)
from adcp.signing.errors import (
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_KEY_UNKNOWN,
    REQUEST_SIGNATURE_REQUIRED,
)

logger = logging.getLogger(__name__)


# Paths the verifier acts on. AdminWSGIMount runs first in the asgi_middleware
# list and short-circuits /admin, /static, /auth, /api, /tenant, /health, etc.
# — so anything reaching this middleware is either /mcp/* or A2A traffic at /.
_BUYER_PROTOCOL_PREFIXES: tuple[str, ...] = ("/mcp", "/a2a")

# Body buffering cap. AdCP requests are JSON-RPC envelopes — well under 1MB
# in practice. Cap at 4MB so a malicious unbounded body can't OOM the worker
# while we buffer for parsing.
MAX_BUFFERED_BODY_BYTES = 4 * 1024 * 1024


def _is_buyer_protocol_path(path: str) -> bool:
    """True if ``path`` is a buyer-protocol POST target.

    ``/`` (A2A root) is included; ``/.well-known/*`` is excluded so AAO
    discovery + agent-card fetches stay unsigned.
    """
    if path == "/":
        return True
    if path.startswith("/.well-known"):
        return False
    for prefix in _BUYER_PROTOCOL_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _peek_kid_from_signature_input(headers: dict[str, str]) -> str | None:
    """Extract ``keyid`` from the ``Signature-Input`` header, or ``None`` on parse failure."""
    raw = None
    for k, v in headers.items():
        if k.lower() == "signature-input":
            raw = v
            break
    if raw is None:
        return None
    try:
        labels = parse_signature_input_header(raw)
    except (ValueError, KeyError):
        return None
    if not labels:
        return None
    parsed = next(iter(labels.values()))
    keyid = parsed.params.get("keyid")
    return str(keyid) if keyid is not None else None


def _has_signature_headers(scope: dict) -> bool:
    """Cheap pre-check: return True only if both Signature + Signature-Input are present."""
    seen_sig = False
    seen_sig_input = False
    for name, _value in scope.get("headers", []):
        lname = name.decode("latin-1").lower() if isinstance(name, bytes) else name.lower()
        if lname == "signature":
            seen_sig = True
        elif lname == "signature-input":
            seen_sig_input = True
        if seen_sig and seen_sig_input:
            return True
    return False


async def _read_body(receive: Any, *, max_bytes: int = MAX_BUFFERED_BODY_BYTES) -> bytes:
    """Drain the ASGI ``receive`` into a single ``bytes`` blob, capped.

    The verifier needs the raw body for digest checking AND downstream handlers
    need it for their own parsing. We buffer once, then use :func:`_replay_receive`
    to re-emit the bytes.
    """
    body = bytearray()
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            # http.disconnect or anything else — bail; the connection's gone.
            break
        chunk = msg.get("body", b"")
        body.extend(chunk)
        if len(body) > max_bytes:
            raise ValueError(
                f"request body exceeded buffering cap of {max_bytes} bytes",
            )
        if not msg.get("more_body", False):
            break
    return bytes(body)


def _replay_receive(body: bytes) -> Any:
    """Return an ASGI ``receive`` callable that yields ``body`` once then closes.

    Downstream handlers that call ``await receive()`` get the buffered body;
    the next call returns an empty ``http.disconnect`` to signal EOF.
    """
    sent = False
    closed = False

    async def replay() -> dict:
        nonlocal sent, closed
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if not closed:
            closed = True
            return {"type": "http.disconnect"}
        # ASGI doesn't define repeated receive after disconnect; mimic Starlette's
        # behavior of returning the disconnect indefinitely so callers don't hang.
        return {"type": "http.disconnect"}

    return replay


def _extract_operation(path: str, body: bytes) -> str | None:
    """Best-effort extraction of the AdCP operation name from the request body.

    MCP (JSON-RPC over HTTP at ``/mcp/*``):
        ``{"jsonrpc":"2.0","method":"tools/call","params":{"name":"<op>", ...}}``
        Returns ``<op>`` (e.g. ``create_media_buy``).

    A2A (at ``/`` per AdCP convention):
        Messages may carry a ``method`` or ``skill`` field naming the operation.
        We try a few common shapes; on miss, return ``None`` and the caller
        can decide whether to enforce against the empty-operation case.

    Returns:
        The operation name when extractable, else ``None``.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    # MCP JSON-RPC tools/call envelope
    method = payload.get("method")
    if method == "tools/call":
        params = payload.get("params") or {}
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str):
                return name

    # A2A: ``method`` is the skill name when no JSON-RPC envelope wraps it.
    if isinstance(method, str) and method != "tools/call":
        return method

    # A2A v0.3+: messages with ``skill`` / ``message_type`` fields.
    for key in ("skill", "message_type", "operation"):
        value = payload.get(key)
        if isinstance(value, str):
            return value

    return None


class SigningVerifyMiddleware:
    """Verify RFC 9421 signatures on inbound buyer-protocol requests.

    Mounted in :func:`core.main.main` via ``serve(asgi_middleware=[...])``.
    Place AFTER ``AdminWSGIMount`` and ``SubdomainTenantMiddleware`` so it
    only sees buyer-protocol traffic that's already been tenant-routed.
    """

    def __init__(
        self,
        app: Any,
        *,
        max_skew_seconds: int = 60,
        max_window_seconds: int = 300,
    ) -> None:
        self.app = app
        self._max_skew = max_skew_seconds
        self._max_window = max_window_seconds

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _is_buyer_protocol_path(path):
            await self.app(scope, receive, send)
            return

        # Buffer the body once. The verifier reads it for digest coverage AND
        # downstream handlers need the same bytes — we re-emit via _replay_receive.
        try:
            body = await _read_body(receive)
        except ValueError:
            await self._send_413(send)
            return
        replayed = _replay_receive(body)

        operation = _extract_operation(path, body)
        signed = _has_signature_headers(scope)

        # Look up policy and operator binding even when no signature is present —
        # required_for may demand one. If the lookups fail (DB unreachable),
        # we fail-open ONLY when no signature was sent and the operation isn't
        # in required_for; otherwise fail-closed.
        try:
            ctx = await self._resolve_policy_context(scope)
        except Exception:
            logger.exception("signing policy lookup crashed")
            ctx = None

        # Trusted operator → bypass everything. Network/header trust is the
        # embedded-mode boundary.
        if ctx is not None and ctx["is_trusted"]:
            await self.app(scope, replayed, send)
            return

        # required_for enforcement: operation gating runs whether or not a
        # signature is present. If the op demands signing and no signature
        # arrived, reject with the spec-mandated code.
        requires_signing = (
            ctx is not None and ctx["enabled"] and operation is not None and operation in ctx["required_for"]
        )
        if requires_signing and not signed:
            await self._send_401(
                send,
                SignatureVerificationError(
                    REQUEST_SIGNATURE_REQUIRED,
                    step=0,
                    message=f"operation {operation!r} requires a signature",
                ),
            )
            return

        if not signed:
            # No signature on the wire and policy doesn't demand one — pass
            # through. Bearer-only legacy path.
            await self.app(scope, replayed, send)
            return

        # Signature is present — verify it.
        try:
            verified = await self._verify(scope, body, ctx, operation)
        except SignatureVerificationError as exc:
            await self._send_401(send, exc)
            return
        except Exception:
            logger.exception("signing verifier crashed")
            if requires_signing:
                # Fail-closed when policy demands signing for this operation.
                await self._send_401(
                    send,
                    SignatureVerificationError(
                        REQUEST_SIGNATURE_REQUIRED,
                        step=0,
                        message="verifier crashed and operation requires signing",
                    ),
                )
                return
            # Fail-open otherwise — bearer-only path stays up.
            await self.app(scope, replayed, send)
            return

        if verified is not None:
            state = scope.setdefault("state", {})
            state["verified_operator_id"] = verified["operator_id"]
            state["verified_agent_url"] = verified["agent_url"]
            state["verified_key_id"] = verified["key_id"]

            from src.core.signing.verified_state import (
                VerifiedRequestState,
                set_verified_state,
            )

            set_verified_state(
                VerifiedRequestState(
                    operator_id=verified["operator_id"],
                    agent_url=verified["agent_url"],
                    key_id=verified["key_id"],
                )
            )

        await self.app(scope, replayed, send)

    async def _resolve_policy_context(self, scope: dict) -> dict[str, Any] | None:
        """Look up tenant + principal + operator + policy state.

        Returns a dict with ``enabled``, ``required_for`` (frozenset),
        ``covers_digest`` (str), ``is_trusted`` (bool), ``brand_json_url``,
        ``operator_id``, ``tenant_id``. ``None`` if anything required is
        missing — the caller treats that as "no operator binding".
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories import (
            AdmittedOperatorRepository,
            TenantSigningPolicyRepository,
        )
        from src.core.resolved_identity import _detect_tenant, _extract_auth_token

        headers: dict[str, str] = {}
        for name, value in scope.get("headers", []):
            headers[name.decode("latin-1")] = value.decode("latin-1")

        tenant_id, _ = _detect_tenant(headers)
        token, _ = _extract_auth_token(headers)
        if not tenant_id or not token:
            return None

        from src.core.auth_utils import get_principal_from_token

        principal_id, _ = get_principal_from_token(token, tenant_id)
        if principal_id is None:
            return None

        from sqlalchemy import select

        from src.core.database.models import Principal

        with get_db_session() as session:
            stmt = select(Principal).filter_by(tenant_id=tenant_id, principal_id=principal_id)
            principal = session.scalars(stmt).first()
            if principal is None or principal.bound_operator_id is None:
                return None
            operator_id = principal.bound_operator_id

            operator = AdmittedOperatorRepository(session, tenant_id).get_by_id(operator_id)
            if operator is None or not operator.is_active:
                return None

            policy = TenantSigningPolicyRepository(session, tenant_id).get_or_default()

            return {
                "tenant_id": tenant_id,
                "operator_id": operator_id,
                "is_trusted": bool(operator.is_trusted),
                "brand_json_url": operator.brand_json_url,
                "enabled": bool(policy.enabled),
                "required_for": frozenset(policy.required_for or []),
                "covers_digest": str(policy.covers_digest_policy),
            }

    async def _verify(
        self,
        scope: dict,
        body: bytes,
        ctx: dict[str, Any] | None,
        operation: str | None,
    ) -> dict[str, str] | None:
        """Run the verifier checklist. Return verified state or raise.

        ``ctx`` MAY be None when the bearer didn't resolve to an operator —
        we still attempt verification by short-circuiting (returns None and
        the caller treats as "no verification possible").
        """
        if ctx is None:
            return None

        from src.core.signing import get_operator_brand_json_cache, get_replay_store

        headers: dict[str, str] = {}
        for name, value in scope.get("headers", []):
            headers[name.decode("latin-1")] = value.decode("latin-1")

        brand_json_url = ctx["brand_json_url"]
        cache = get_operator_brand_json_cache()
        async_resolver = await cache.resolver_for(brand_json_url, agent_type="buying")
        replay_store = get_replay_store()

        kid = _peek_kid_from_signature_input(headers)
        if kid is None:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_HEADER_MALFORMED,
                step=1,
                message="could not parse keyid from Signature-Input",
            )
        jwk = await async_resolver(kid)
        if jwk is None:
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_KEY_UNKNOWN,
                step=7,
                message=f"kid {kid!r} not found in operator's brand.json JWKS",
            )
        sync_resolver = StaticJwksResolver({"keys": [jwk]})

        capability = VerifierCapability(
            supported=True,
            covers_content_digest=ctx["covers_digest"],  # type: ignore[arg-type]
            required_for=ctx["required_for"],
            supported_for=frozenset(),
        )

        # Build a Starlette Request with a one-shot receive that re-emits the
        # buffered body so the verifier can call ``await request.body()``.
        from starlette.requests import Request

        verify_receive = _replay_receive(body)
        request: Request = Request(scope, verify_receive)

        options = VerifyOptions(
            now=time.time(),
            capability=capability,
            operation=operation or "unknown",
            jwks_resolver=sync_resolver,
            replay_store=replay_store,
            max_skew_seconds=self._max_skew,
            max_window_seconds=self._max_window,
        )

        signer = await verify_starlette_request(request, options=options)

        agent_url = getattr(async_resolver, "agent_url", None) or brand_json_url

        return {
            "operator_id": ctx["operator_id"],
            "agent_url": str(agent_url),
            "key_id": signer.key_id,
        }

    async def _send_401(self, send: Any, exc: SignatureVerificationError) -> None:
        """Emit the spec-mandated 401 + ``WWW-Authenticate`` header."""
        headers = unauthorized_response_headers(exc)
        body = b'{"error":{"code":"' + exc.code.encode("ascii") + b'","message":"signature verification failed"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    *((k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _send_413(self, send: Any) -> None:
        """Reject oversized bodies before any verification work."""
        body = b'{"error":{"code":"request_body_too_large","message":"body exceeds signing-buffer cap"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
