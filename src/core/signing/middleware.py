"""Starlette ASGI middleware for inbound RFC 9421 signature verification.

PR 2B of [signing-non-embedded](../../../../docs/design/signing-non-embedded.md).

Runs before MCP and A2A buyer-protocol handlers. When a signed request arrives,
verifies it against the buyer's brand.json (resolved through the AdCP discovery
chain by ``adcp.signing.BrandJsonJwksResolver``) and attaches the verified
operator/agent/key state to the ASGI scope so downstream identity resolution
can record it on ``ResolvedIdentity`` and ``AuditLog``.

PR 2B v1 semantics:

* Trusted operators (embedded host's interchange) bypass verification entirely.
* Bearer + signature: bearer pins the operator; signature proves the call came
  from one of the operator's authorized agents. Both required when a signature
  is present.
* When no signature is present, the middleware is a no-op. Per-operation
  enforcement of ``TenantSigningPolicy.required_for`` lands in PR 2C, which
  needs to peek at the request body to extract the AdCP operation name.

Failure mapping:

* Signature parse / window / replay / unknown-key → 401 with
  ``WWW-Authenticate: Signature error="<spec-code>"``
* Bearer maps to a principal whose ``bound_operator_id`` is missing → request
  passes through unchanged (legacy bearer-only path); the operator-pinning
  enforcement is in PR 2C alongside ``required_for``.
* Backend errors (DB unreachable, brand.json fetch failure on a hot path) →
  fall through (fail-open) so an outage in the signing-side infrastructure
  doesn't take the bearer-only paths down with it. PR 2C tightens this to
  fail-closed when ``required_for`` mandates signing for the operation.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

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
)

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)


# Paths the verifier acts on. AdminWSGIMount runs first in the asgi_middleware
# list and short-circuits /admin, /static, /auth, /api, /tenant, /health, etc.
# — so anything reaching this middleware is either /mcp/* or A2A traffic at /.
# We only attempt verification on these paths to keep startup-probe + landing
# traffic untouched.
_BUYER_PROTOCOL_PREFIXES: tuple[str, ...] = ("/mcp", "/a2a")


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
    """Extract ``keyid`` from the ``Signature-Input`` header, or ``None`` on parse failure.

    The library's :func:`parse_signature_input_header` does the structured-fields
    parsing for us. We use it as a one-shot to learn which kid the signer
    claims so we can pre-resolve the JWK before handing off to the sync verifier.
    """
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
    # The library defaults the label to "sig" — but a buyer may pick any.
    # Take the first label's keyid; if a buyer signs with multiple labels,
    # PR 2C extends this to handle them.
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

        if not _has_signature_headers(scope):
            # No signature on the wire — nothing to verify. PR 2C will gate on
            # TenantSigningPolicy.required_for here.
            await self.app(scope, receive, send)
            return

        try:
            verified = await self._verify(scope, receive)
        except SignatureVerificationError as exc:
            await self._send_401(send, exc)
            return
        except Exception:
            # Defense-in-depth: a bug in our verifier path must not take down
            # bearer-only traffic. Log loudly and fall through.
            logger.exception("signing verifier crashed; falling through to bearer-only auth")
            await self.app(scope, receive, send)
            return

        if verified is not None:
            # Attach to both the ASGI scope (Starlette/A2A handlers can read
            # ``request.state``) AND a contextvar (FastMCP per-tool-call
            # middleware reads via ``get_verified_state()``). Both are
            # request-scoped; the contextvar avoids coupling the FastMCP
            # boundary to ASGI scope shape.
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

        await self.app(scope, receive, send)

    async def _verify(self, scope: dict, receive: Any) -> dict[str, str] | None:
        """Run the verifier checklist. Return verified state or ``None``.

        ``None`` means we couldn't construct a verifier (e.g. principal not
        bound to an operator) — equivalent to "no signature present" at the
        scope level, deferred to PR 2C for stricter handling.
        """
        from src.core.database.database_session import get_db_session
        from src.core.database.repositories import (
            AdmittedOperatorRepository,
            TenantSigningPolicyRepository,
        )
        from src.core.resolved_identity import _detect_tenant, _extract_auth_token
        from src.core.signing import get_operator_brand_json_cache, get_replay_store

        # Build a mutable headers dict the rest of the salesagent expects.
        headers: dict[str, str] = {}
        for name, value in scope.get("headers", []):
            headers[name.decode("latin-1")] = value.decode("latin-1")

        tenant_id, _ = _detect_tenant(headers)
        token, _ = _extract_auth_token(headers)
        if not tenant_id or not token:
            return None

        # Resolve operator binding. Heavy import deferred to keep middleware
        # startup cheap.
        from src.core.auth_utils import get_principal_from_token

        principal_id, _ = get_principal_from_token(token, tenant_id)
        if principal_id is None:
            return None

        with get_db_session() as session:
            principal = session.get_bind() and None  # placeholder
            # Direct row fetch — repo for principals lives elsewhere; one-shot
            # query keeps this isolated. Read-only, transient session.
            from sqlalchemy import select

            from src.core.database.models import Principal

            stmt = select(Principal).filter_by(tenant_id=tenant_id, principal_id=principal_id)
            principal = session.scalars(stmt).first()
            if principal is None or principal.bound_operator_id is None:
                # Bearer-only legacy path. Don't enforce signing yet (PR 2C).
                return None
            operator_id = principal.bound_operator_id

            operator_repo = AdmittedOperatorRepository(session, tenant_id)
            operator = operator_repo.get_by_id(operator_id)
            if operator is None or not operator.is_active:
                # Operator gone or disabled. The bearer token itself shouldn't
                # have authenticated — but defense-in-depth: don't verify.
                return None
            if operator.is_trusted:
                # Embedded host's interchange — never verify, trust the network.
                return None

            policy = TenantSigningPolicyRepository(session, tenant_id).get_or_default()
            brand_json_url = operator.brand_json_url

        capability = VerifierCapability(
            supported=True,
            covers_content_digest=policy.covers_digest_policy,  # type: ignore[arg-type]
            required_for=frozenset(policy.required_for or []),
            supported_for=frozenset(),
        )

        cache = get_operator_brand_json_cache()
        async_resolver = await cache.resolver_for(brand_json_url, agent_type="buying")
        replay_store = get_replay_store()

        # Pre-resolve the JWK async (BrandJsonJwksResolver is async-only) and
        # pass a sync StaticJwksResolver to the verifier. The library's
        # verify_request_signature is sync; this is the canonical pattern for
        # using an async resolver underneath it.
        kid = _peek_kid_from_signature_input(headers)
        if kid is None:
            # Bad headers — let the library's verifier surface the precise code.
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

        # Build a Starlette Request-shaped object the library accepts.
        from starlette.requests import Request

        request: Request = Request(scope, receive)

        # PR 2C: extract AdCP operation name from the request body for
        # required_for enforcement. For now use a sentinel that's not in any
        # tenant's required_for list.
        operation = "unknown"

        options = VerifyOptions(
            now=time.time(),
            capability=capability,
            operation=operation,
            jwks_resolver=sync_resolver,
            replay_store=replay_store,
            max_skew_seconds=self._max_skew,
            max_window_seconds=self._max_window,
        )

        signer = await verify_starlette_request(request, options=options)

        # Derive the verified agent_url from the matched brand.json entry. The
        # AsyncCachingJwksResolver inside BrandJsonJwksResolver exposes
        # ``agent_url`` after a successful resolve — the lib's docstring
        # promises it's populated when known.
        agent_url = getattr(async_resolver, "agent_url", None) or brand_json_url

        return {
            "operator_id": operator_id,
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
