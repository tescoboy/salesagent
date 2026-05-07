"""Regression test: a successfully-synced creative MUST appear in
``list_creatives`` for the same principal.

Issue #88: ``tests/e2e/test_creative_assignment_e2e.py::test_creative_sync_with_assignment_in_single_call``
fails at line 221 with "Creative e2etestcreative_xxx should be in list" —
the test syncs a creative (sync responds with action), then calls
list_creatives and the creative isn't in the response.

This integration test reproduces the flow at the impl layer with real
DB to narrow whether the bug is:
* sync silently failing to persist (action says created but no DB row)
* list filtering by criteria that exclude the just-synced creative
* tenant/principal scoping mismatch between the two calls
"""

from __future__ import annotations

import pytest

from adcp.types import CreativeAction
from adcp.types import FormatId as AdcpFormatId
from adcp.types.generated_poc.core.creative_asset import CreativeAsset

from tests.harness import CreativeListEnv, CreativeSyncEnv

DEFAULT_AGENT_URL = "https://creative.adcontextprotocol.org"

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _build_test_creative(creative_id: str = "test_creative_001") -> CreativeAsset:
    """Build a minimal valid CreativeAsset matching the e2e test's payload shape."""
    return CreativeAsset(
        creative_id=creative_id,
        name="E2E Test Creative",
        format_id=AdcpFormatId(agent_url=DEFAULT_AGENT_URL, id="display_html"),
        assets={"primary": {"asset_type": "url", "url": "https://example.com/test-banner.png"}},
    )


class TestSyncedCreativeAppearsInList:
    """Synced creatives must be visible to subsequent list_creatives calls
    by the same principal in the same tenant.

    Covers: #88
    """

    def test_synced_creative_appears_in_subsequent_list(self, integration_db):
        """After sync_creatives reports action=created, list_creatives must
        return the same creative_id."""
        creative_id = "synced_001"
        creative = _build_test_creative(creative_id=creative_id)

        # Phase 1: sync inside its env (creates tenant/principal/db state).
        with CreativeSyncEnv() as env:
            env.setup_default_data()
            sync_response = env.call_impl(creatives=[creative])

            # Capture the principal/tenant the sync ran under so list runs in
            # the same scope.
            sync_tenant_id = env.identity.tenant_id
            sync_principal_id = env.identity.principal_id

        # Sanity: sync claimed success.
        assert len(sync_response.creatives) == 1
        result = sync_response.creatives[0]
        assert result.action == CreativeAction.created, (
            f"Sync did not create the creative — action={result.action}, errors={getattr(result, 'errors', None)}"
        )
        assert result.creative_id == creative_id

        # Phase 2: list under the same principal/tenant. The DB rows from
        # phase 1 are persisted — list must see them.
        with CreativeListEnv(tenant_id=sync_tenant_id, principal_id=sync_principal_id) as env:
            list_response = env.call_impl()

        returned_ids = {c.creative_id for c in list_response.creatives}
        assert creative_id in returned_ids, (
            f"Synced creative {creative_id!r} not in list response. "
            f"Got {returned_ids}. "
            f"This is the bug from #88 — list_creatives doesn't see freshly-synced rows."
        )
