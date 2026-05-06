"""
Multi-Tenant Isolation E2E Tests

Validates that tenant data isolation works correctly through the full HTTP stack
(nginx proxy -> FastAPI -> PostgreSQL -> MCP response).

Core invariant: A tenant's MCP tools must only return data belonging to that
tenant; cross-tenant tokens must be rejected at the transport boundary.

These tests require two tenants in the database:
- ci-test: The primary test tenant (created by init_database_ci.py)
- iso-test: The isolation test tenant (created by init_database_ci.py)

Note: These tests use x-adcp-tenant header for tenant selection because
DNS-based subdomain routing is not available in the CI Docker stack.
Integration tests in test_tenant_isolation_breach_fix.py cover Host-based
subdomain routing separately.
"""

import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.e2e.adcp_request_builder import parse_tool_result


class TestMultiTenantIsolation:
    """Verify that MCP tool responses are scoped to the requesting tenant."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_tenant_a_only_sees_own_products(self, docker_services_e2e, live_server):
        """ci-test tenant should only see products belonging to ci-test, not iso-test.

        Exercises: x-adcp-tenant header -> tenant resolution -> get_products -> tenant-scoped query.
        """
        headers = {
            "x-adcp-auth": "ci-test-token",
            "x-adcp-tenant": "ci-test",
        }
        transport = StreamableHttpTransport(
            url=f"{live_server['mcp']}/mcp/",
            headers=headers,
        )

        async with Client(transport=transport) as client:
            result = await client.call_tool(
                "get_products",
                {"brief": "all products", "context": {"e2e": "tenant_isolation"}},
            )
            data = parse_tool_result(result)

            assert "products" in data, "Response must contain products key"
            products = data["products"]
            assert len(products) > 0, "ci-test tenant must have at least one product"

            # Verify ALL returned products belong to ci-test (product IDs start with prod_)
            product_ids = [p["product_id"] for p in products]
            for pid in product_ids:
                assert not pid.startswith("iso_"), (
                    f"ci-test tenant received iso-test product: {pid}. Tenant isolation breach detected."
                )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_tenant_b_only_sees_own_products(self, docker_services_e2e, live_server):
        """iso-test tenant should only see products belonging to iso-test, not ci-test.

        Exercises: x-adcp-tenant header -> tenant resolution -> get_products -> tenant-scoped query.
        """
        headers = {
            "x-adcp-auth": "iso-test-token",
            "x-adcp-tenant": "iso-test",
        }
        transport = StreamableHttpTransport(
            url=f"{live_server['mcp']}/mcp/",
            headers=headers,
        )

        async with Client(transport=transport) as client:
            result = await client.call_tool(
                "get_products",
                {"brief": "all products", "context": {"e2e": "tenant_isolation"}},
            )
            data = parse_tool_result(result)

            assert "products" in data, "Response must contain products key"
            products = data["products"]
            assert len(products) > 0, "iso-test tenant must have at least one product"

            # Verify ALL returned products belong to iso-test (product IDs start with iso_)
            product_ids = [p["product_id"] for p in products]
            for pid in product_ids:
                assert pid.startswith("iso_"), (
                    f"iso-test tenant received non-iso product: {pid}. Tenant isolation breach detected."
                )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cross_tenant_header_is_ignored_token_wins(self, docker_services_e2e, live_server):
        """``x-adcp-tenant`` header is advisory and grants no access — the
        bearer token's bound tenant is the only thing that selects scope.

        The legacy stack (deleted in PR #17) honoured ``x-adcp-tenant`` for
        path-based tenant routing, so a header-injection attack was a real
        concern: a buyer holding a ci-test token could *attempt* to set
        ``x-adcp-tenant: iso-test``, and the legacy stack relied on the
        token-validation step to reject the mismatch.

        The modern stack (``adcp.server.serve()`` +
        :class:`BearerTokenAuthMiddleware`) takes a stronger position: the
        ``x-adcp-tenant`` header is not consumed at all. Tenant scope is
        derived from ``Principal.access_token`` lookup
        (:func:`core.main._validate_token` returns
        ``Principal(tenant_id=row.tenant_id)``), and the auth middleware
        seeds ``current_tenant`` from that. Header-injection is a
        non-attack: the buyer's own tenant is what they get, regardless of
        what the header claims.

        This test asserts the post-deletion contract: send a misleading
        header, verify the response is scoped to the token's tenant
        (ci-test), NOT the header's claim (iso-test). If a future change
        ever wires the ``x-adcp-tenant`` header back into tenant selection,
        this test will catch it — the response would either start showing
        iso-test products or rejecting outright, both of which break the
        assertion below.

        Exercises: bearer token → ``current_tenant`` ContextVar → tenant
        scoping in ``_get_products_impl``.
        """
        headers = {
            "x-adcp-auth": "ci-test-token",  # Token belongs to ci-test
            "x-adcp-tenant": "iso-test",  # Misleading header — must be ignored
        }
        transport = StreamableHttpTransport(
            url=f"{live_server['mcp']}/mcp/",
            headers=headers,
        )

        async with Client(transport=transport) as client:
            result = await client.call_tool(
                "get_products",
                {"brief": "header injection should not change scope", "context": {"e2e": "cross_tenant"}},
            )
            data = parse_tool_result(result)

        # Token wins: the response is scoped to ci-test (the token's bound
        # tenant), not iso-test (the header's claim). ci-test products
        # have IDs that do NOT start with ``iso_``.
        assert "products" in data, "Response must contain products key"
        products = data["products"]
        assert len(products) > 0, (
            "ci-test tenant must have at least one product — confirms request was "
            "scoped to the token's tenant, not the header's claim."
        )
        product_ids = [p["product_id"] for p in products]
        for pid in product_ids:
            assert not pid.startswith("iso_"), (
                f"Header-injection breach: x-adcp-tenant: iso-test caused iso-test "
                f"product {pid} to surface for a ci-test token. The modern stack "
                f"must IGNORE x-adcp-tenant; only the token's bound tenant grants scope."
            )
