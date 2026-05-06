"""Base test environment for _impl function testing.

Unified base for both integration and unit test environments:

- **Integration mode** (``use_real_db = True``): Creates a non-scoped SQLAlchemy
  session, binds factory_boy factories, only mocks external services.
  Requires ``integration_db`` pytest fixture.
- **Unit mode** (``use_real_db = False``): No database setup, patches all
  dependencies including DB.

Subclasses override:
    EXTERNAL_PATCHES: dict[str, str]   -- {name: patch_target} for mocks
    _configure_mocks(): None           -- wire mock defaults
    call_impl(**kwargs): Any           -- call production function

Multi-transport support: subclasses may override ``call_mcp(**kwargs)``
to dispatch through the in-process MCP server (``Transport.MCP``).
``Transport.IMPL`` calls ``call_impl`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.core.resolved_identity import ResolvedIdentity
    from tests.harness.transport import Transport, TransportResult


def _adcp_error_from_code(
    error_code: str,
    message: str,
    recovery: str | None = None,
    details: dict | None = None,
) -> Exception:
    """Reconstruct the exact AdCPError subclass from an error_code string.

    Shared by MCP and A2A unwrappers. Maps error codes like 'NOT_FOUND'
    to AdCPNotFoundError, 'VALIDATION_ERROR' to AdCPValidationError, etc.
    Falls back to base AdCPError for unknown codes.
    """
    from src.core.exceptions import (
        AdCPAccountAmbiguousError,
        AdCPAccountNotFoundError,
        AdCPAccountPaymentRequiredError,
        AdCPAccountSetupRequiredError,
        AdCPAccountSuspendedError,
        AdCPAdapterError,
        AdCPAuthenticationError,
        AdCPAuthorizationError,
        AdCPBudgetExhaustedError,
        AdCPConflictError,
        AdCPError,
        AdCPNotFoundError,
        AdCPRateLimitError,
        AdCPServiceUnavailableError,
        AdCPValidationError,
    )

    _CODE_TO_CLASS: dict[str, type[AdCPError]] = {
        cls.error_code: cls
        for cls in (
            AdCPValidationError,
            AdCPAuthenticationError,
            AdCPAuthorizationError,
            AdCPNotFoundError,
            AdCPAccountNotFoundError,
            AdCPAccountSetupRequiredError,
            AdCPAccountSuspendedError,
            AdCPAccountPaymentRequiredError,
            AdCPConflictError,
            AdCPAccountAmbiguousError,
            AdCPBudgetExhaustedError,
            AdCPRateLimitError,
            AdCPAdapterError,
            AdCPServiceUnavailableError,
        )
    }
    exc_cls = _CODE_TO_CLASS.get(error_code, AdCPError)
    reconstructed = exc_cls(
        message=message,
        details=details,
        recovery=recovery or "terminal",
    )
    if exc_cls is AdCPError:
        reconstructed.error_code = error_code
    return reconstructed


def _unwrap_mcp_tool_error(exc: Exception) -> Exception:
    """Translate FastMCP ToolError back to the corresponding AdCPError.

    The MCP tool wrappers (via with_error_logging) convert AdCPError to
    ToolError(error_code, message, recovery). When the error travels through
    the MCP Client, the structured args are serialized to a single string:
    ``"('VALIDATION_ERROR', 'message', 'correctable')"``.

    This parses the string back to a tuple via ast.literal_eval and
    reconstructs the AdCPError subclass.

    If the exception is not a ToolError or can't be parsed, returns it unchanged.
    """
    import ast

    from fastmcp.exceptions import ToolError

    if not isinstance(exc, ToolError):
        return exc

    # ToolError from Client has a single string arg containing the repr'd tuple.
    error_str = str(exc)

    # Try to parse as a Python tuple: ('CODE', 'message', 'recovery', '{"details": ...}')
    try:
        parsed = ast.literal_eval(error_str)
        if isinstance(parsed, tuple) and len(parsed) >= 2:
            error_code = str(parsed[0])
            message = str(parsed[1])
            recovery = str(parsed[2]) if len(parsed) > 2 else None

            # 4th element is JSON-serialized details dict (if present)
            details = None
            if len(parsed) > 3 and parsed[3] is not None:
                import json

                try:
                    details = json.loads(str(parsed[3]))
                except (json.JSONDecodeError, TypeError):
                    pass

            return _adcp_error_from_code(error_code, message, recovery, details)
    except (ValueError, SyntaxError):
        pass

    # Fallback: try extract_error_info (handles direct ToolError construction)
    from src.core.tool_error_logging import extract_error_info

    error_code, message, recovery = extract_error_info(exc)
    if error_code != "TOOL_ERROR":
        return _adcp_error_from_code(error_code, message, recovery)

    return exc


class BaseTestEnv:
    """Base test environment for _impl function testing.

    Subclasses define:
        EXTERNAL_PATCHES: dict[str, str]   -- {name: patch_target}
        _configure_mocks(): None           -- wire mock defaults
        call_impl(**kwargs): Any           -- call production function

    Set ``use_real_db = True`` in integration subclasses to enable
    factory_boy session binding.

    Usage (integration)::

        @pytest.mark.requires_db
        def test_something(self, integration_db):
            with DeliveryPollEnv() as env:
                tenant = TenantFactory(tenant_id="t1")
                response = env.call_impl(media_buy_ids=["mb_001"])

    Usage (unit)::

        with DeliveryPollEnvUnit() as env:
            env.add_buy(media_buy_id="mb_001")
            response = env.call_impl(media_buy_ids=["mb_001"])

    Usage (multi-transport)::

        @pytest.mark.parametrize("transport", [Transport.IMPL, Transport.MCP])
        def test_something(self, integration_db, transport):
            with CreativeSyncEnv() as env:
                result = env.call_via(transport, creatives=[...])
                assert result.is_success

    Attributes:
        mock: dict[str, MagicMock]  -- active mocks keyed by short name
        identity: ResolvedIdentity  -- default identity (override via constructor)
    """

    EXTERNAL_PATCHES: dict[str, str] = {}
    ASYNC_PATCHES: set[str] = set()  # Names that need AsyncMock (for async functions)
    MODULE: str = ""  # Convenience for unit envs building patch paths
    use_real_db: bool = False

    def __init__(
        self,
        principal_id: str = "test_principal",
        tenant_id: str = "test_tenant",
        dry_run: bool = False,
        **tenant_overrides: Any,
    ) -> None:
        self._principal_id = principal_id
        self._tenant_id = tenant_id
        self._dry_run = dry_run
        self._tenant_overrides = tenant_overrides
        self.mock: dict[str, MagicMock] = {}
        self._patchers: list[Any] = []
        self._session: Session | None = None
        self._identity_cache: dict[str, ResolvedIdentity] = {}

    # -- Identity (one function, all transports) ----------------------------

    def identity_for(self, transport: Transport) -> ResolvedIdentity:
        """Build ResolvedIdentity with the correct protocol for *transport*.

        This is the single source of truth for test identity across all
        transports. The identity is cached per protocol so repeated calls
        with the same transport return the same object.

        In integration mode (``use_real_db=True``), the identity carries
        the real ``auth_token`` from the factory-created Principal row.
        This enables full auth chain testing: header → token → DB lookup.
        """
        from tests.harness.transport import TRANSPORT_PROTOCOL

        protocol = TRANSPORT_PROTOCOL[transport]
        if protocol not in self._identity_cache:
            from tests.factories.principal import PrincipalFactory

            # In integration mode, commit factory data first so the token
            # is visible to other sessions (e.g., get_principal_from_token
            # in the MCP auth chain uses a separate get_db_session() call).
            auth_token = None
            if self.use_real_db:
                self._commit_factory_data()
                auth_token = self._resolve_auth_token()

            self._identity_cache[protocol] = PrincipalFactory.make_identity(
                principal_id=self._principal_id,
                tenant_id=self._tenant_id,
                protocol=protocol,
                dry_run=self._dry_run,
                auth_token=auth_token,
                **self._tenant_overrides,
            )
        return self._identity_cache[protocol]

    def _resolve_auth_token(self) -> str | None:
        """Look up the real access_token from the session-bound Principal.

        Only called in integration mode where ``self._session`` is bound
        to factory-created ORM models. Returns None if the principal
        hasn't been created yet (identity built before Given steps run).
        """
        if not self._session:
            return None
        from sqlalchemy import select

        from src.core.database.models import Principal

        token = self._session.scalars(
            select(Principal.access_token).filter_by(
                principal_id=self._principal_id,
                tenant_id=self._tenant_id,
            )
        ).first()
        return token

    @property
    def identity(self) -> ResolvedIdentity:
        """Default identity (protocol='mcp'). Backward-compatible.

        Supports direct override via ``env._identity = ...`` for integration
        tests that create tenants in the DB and need LazyTenantContext.
        """
        # Backward compat: tests may set env._identity directly
        direct = self.__dict__.get("_identity")
        if direct is not None:
            return direct
        from tests.harness.transport import Transport

        return self.identity_for(Transport.IMPL)

    # -- Transport dispatch -------------------------------------------------

    def call_via(self, transport: Transport, **kwargs: Any) -> TransportResult:
        """Dispatch through *transport* and return normalized TransportResult.

        Injects the correct identity for the transport into kwargs (unless
        the caller explicitly provides one). Routes to the appropriate
        dispatcher.
        """
        from tests.harness.dispatchers import DISPATCHERS

        # Inject transport-correct identity
        kwargs.setdefault("identity", self.identity_for(transport))

        dispatcher = DISPATCHERS[transport]
        return dispatcher.dispatch(self, **kwargs)

    # -- Per-transport hooks (override in subclass) -------------------------

    def _configure_mocks(self) -> None:
        """Wire up happy-path return values on self.mock entries.

        Called automatically after all patches are started.
        Override in subclass.
        """

    def call_impl(self, **kwargs: Any) -> Any:
        """Call the production function under test.

        Override in subclass. Should construct the request object
        and call the _impl function.
        """
        raise NotImplementedError

    def call_mcp(self, **kwargs: Any) -> Any:
        """Call the async MCP wrapper with a mock Context.

        Override in subclass. Should create a mock Context with
        get_state("identity") returning the MCP identity, call the
        async MCP wrapper, and extract the payload from ToolResult.structured_content.

        Note on enum coercion: FastMCP auto-coerces string values to enums
        when calling tools through the MCP protocol. When calling wrappers
        directly in tests, you must coerce enum parameters yourself before
        passing them. See CreativeSyncEnv.call_mcp for an example with
        ValidationMode.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement call_mcp(). Override to enable Transport.MCP dispatch."
        )

    def _run_mcp_client(
        self,
        tool_name: str,
        response_cls: type,
        **kwargs: Any,
    ) -> Any:
        """MCP dispatch via httpx ``ASGITransport`` against ``core.main.build_app()``.

        Drives the full production pipeline: bearer-token middleware,
        FastMCP streamable-http transport, tool dispatcher, response
        envelope. The same Starlette app production binds with uvicorn
        runs in-process — no socket, but every middleware and validation
        hook is exercised.

        Identity resolution:

        * **Integration mode** (factory-created Principal with real
          ``access_token``): the harness sends ``x-adcp-auth: <token>``
          and the bearer middleware looks it up via ``_validate_token``
          → DB → :class:`Principal` → ContextVars consumed by
          ``auth_context_factory``. Tests exercise the same auth chain
          a real buyer hits.
        * **Unit mode** (no token): the harness patches
          ``resolve_identity_from_context`` so the wrapper layer
          synthesises identity from the in-memory mock. The bearer
          middleware accepts the missing/invalid token, but the patched
          resolver overrides identity downstream so business logic still
          sees a populated identity.

        Args:
            tool_name: MCP tool name (e.g., "get_products").
            response_cls: Pydantic model class to parse structured_content into.
            **kwargs: Tool arguments. ``identity`` is popped and used for
                the auth chain; ``req`` is popped and its fields unpacked
                into the arguments dict.
        """
        from unittest.mock import patch

        import httpx
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        from tests.harness._asgi_app import run_on_app_loop
        from tests.harness.transport import Transport

        self._commit_factory_data()

        _NO_OVERRIDE = object()
        identity = kwargs.pop("identity", _NO_OVERRIDE)
        mcp_identity = self.identity_for(Transport.MCP) if identity is _NO_OVERRIDE else identity

        # Unpack req object into flat arguments — MCP tools accept individual
        # params, not a request model.
        req = kwargs.pop("req", None)
        if req is not None and hasattr(req, "model_dump"):
            req_fields = req.model_dump(exclude_none=True)
            arguments = {**req_fields, **kwargs}
        else:
            arguments = dict(kwargs)

        auth_token = mcp_identity.auth_token if mcp_identity else None

        # Always send a token — when no real one exists (unit mode), use a
        # placeholder so the bearer middleware short-circuits past the
        # missing-header path. The patched ``resolve_identity_from_context``
        # supplies the real identity.
        request_headers = {
            "x-adcp-auth": auth_token or "test-stub-token",
        }
        if mcp_identity and mcp_identity.tenant_id:
            request_headers["x-adcp-tenant"] = mcp_identity.tenant_id

        def _factory(app: Any):
            def httpx_factory(**hk: Any) -> httpx.AsyncClient:
                hk.setdefault("timeout", 30.0)
                hk["transport"] = httpx.ASGITransport(app=app)
                hk["base_url"] = "http://testserver"
                return httpx.AsyncClient(**hk)

            transport = StreamableHttpTransport(
                url="http://testserver/mcp/",
                headers=request_headers,
                httpx_client_factory=httpx_factory,
            )

            async def _call() -> Any:
                async with Client(transport) as client:
                    result = await client.call_tool(tool_name, arguments)
                    return response_cls(**result.structured_content)

            return _call()

        try:
            if not auth_token:
                # Unit mode: inject identity at the wrapper layer, since no
                # real Principal row exists for the bearer middleware to find.
                with patch(
                    "src.core.mcp_auth_middleware.resolve_identity_from_context",
                    return_value=mcp_identity,
                ):
                    return run_on_app_loop(_factory)
            return run_on_app_loop(_factory)
        except Exception as exc:
            raise _unwrap_mcp_tool_error(exc) from exc

    def _run_mcp_wrapper(
        self,
        wrapper_fn: Any,
        response_cls: type,
        **kwargs: Any,
    ) -> Any:
        """Legacy MCP dispatch: mock Context → async wrapper → parse response.

        .. deprecated::
            Use ``_run_mcp_client`` instead for full-pipeline dispatch.
            This method bypasses FastMCP middleware and TypeAdapter validation.
            Kept for unit-mode envs that cannot use the in-memory Client.
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from fastmcp.server.context import Context

        from tests.harness.transport import Transport

        self._commit_factory_data()

        _NO_OVERRIDE = object()
        identity = kwargs.pop("identity", _NO_OVERRIDE)
        mcp_identity = self.identity_for(Transport.MCP) if identity is _NO_OVERRIDE else identity

        # Unpack req object into flat kwargs — MCP wrappers accept individual
        # parameters, not a request model.
        req = kwargs.pop("req", None)
        if req is not None and hasattr(req, "model_dump"):
            req_fields = req.model_dump(exclude_none=True)
            # kwargs override req fields (explicit > implicit)
            kwargs = {**req_fields, **kwargs}

        mock_ctx = MagicMock(spec=Context)
        mock_ctx.get_state = AsyncMock(return_value=mcp_identity)

        tool_result = asyncio.run(wrapper_fn(ctx=mock_ctx, **kwargs))
        return response_cls(**tool_result.structured_content)

    def _commit_factory_data(self) -> None:
        """Flush pending session state before calling production code.

        Factories use ``sqlalchemy_session_persistence = "commit"`` and auto-commit
        each model creation. This explicit commit ensures any cascading saves or
        deferred flushes are visible to production code's separate database session.
        Called automatically by call_impl() before each test execution.
        """
        if self._session:
            self._session.commit()

    # -- Context manager protocol ------------------------------------------

    def __enter__(self) -> Self:
        # 1. Database setup (integration mode only)
        if self.use_real_db:
            from sqlalchemy.orm import Session as SASession

            from src.core.database.database_session import get_engine
            from tests.factories import ALL_FACTORIES

            # Guard against nested envs — session binding is global
            for f in ALL_FACTORIES:
                assert f._meta.sqlalchemy_session is None, (
                    f"Factory {getattr(f, '__name__', type(f).__name__)} session already bound — "
                    "nested IntegrationEnv contexts are not supported"
                )

            engine = get_engine()
            self._session = SASession(bind=engine)

            for f in ALL_FACTORIES:
                f._meta.sqlalchemy_session = self._session

        # 2. Start patches
        for name, target in self.EXTERNAL_PATCHES.items():
            if name in self.ASYNC_PATCHES:
                patcher = patch(target, new_callable=AsyncMock)
            else:
                patcher = patch(target)
            self.mock[name] = patcher.start()
            self._patchers.append(patcher)

        self._configure_mocks()
        return self

    def __exit__(self, *exc: object) -> bool:
        errors: list[Exception] = []

        # 1. Unbind factories (integration mode only)
        if self.use_real_db:
            try:
                from tests.factories import ALL_FACTORIES

                for f in ALL_FACTORIES:
                    f._meta.sqlalchemy_session = None
            except Exception as e:
                errors.append(e)

            try:
                if self._session:
                    self._session.close()
                    self._session = None
            except Exception as e:
                errors.append(e)

        # 2. Stop patches — each in its own try block
        for patcher in reversed(self._patchers):
            try:
                patcher.stop()
            except Exception as e:
                errors.append(e)
        self._patchers.clear()
        self.mock.clear()
        self._identity_cache.clear()

        if errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("Multiple teardown errors", errors)
        return False


class IntegrationEnv(BaseTestEnv):
    """Integration test environment — real database, only mocks external services.

    Requires ``integration_db`` pytest fixture.
    """

    use_real_db = True

    def setup_default_data(self) -> tuple[Any, Any]:
        """Create default tenant + principal via factories.

        Must be called inside the ``with env:`` block (factories are bound
        to the session during ``__enter__``).

        Returns (tenant, principal) ORM instances. Uses self._tenant_id
        and self._principal_id from constructor.
        """
        from tests.factories import PrincipalFactory, TenantFactory

        tenant = TenantFactory(tenant_id=self._tenant_id)
        principal = PrincipalFactory(tenant=tenant, principal_id=self._principal_id)
        return tenant, principal
