"""add agent gam cache table and gam order id column

Combines mollybots migrations 025/026/027 into one local migration. The
cache underwrites the Agent Media Buys UI (gated on
``tenants.agent_media_buys_enabled`` + ``SALESAGENT_FF_AGENT_CACHE``).
Disabled state == today's behavior (table empty and unread).

Schema notes:

- ``media_buys.gam_order_id`` is added to mirror what mollybots stores on
  their ``agent_media_buys`` table. We don't have a separate
  ``agent_media_buys`` table — the cache reads ``media_buys`` directly.
  Indexed for the poller's "give me orders to refresh" query.
- ``agent_gam_cache`` is keyed by ``(tenant_id, order_id)`` — one row per
  GAM Order, holding the latest cumulative totals from
  ``ReportService``. Daily breakdown is intentionally out of scope (matches
  mollybots).
- All metric columns are ``BIGINT NOT NULL DEFAULT 0`` so the poller can
  ``ON CONFLICT (tenant_id, order_id) DO UPDATE`` without partial writes.
- Spec compliance: AdCP ``DeliveryTotals`` + ``PackageDelivery`` already
  carry ``video_completions`` (see ``src/core/schemas/delivery.py:108,138``)
  and ``quartile_data`` (``src/core/schemas/delivery.py:406`` references it).
  This migration adds the storage; the population happens in a follow-up
  in ``src/core/tools/media_buy_delivery.py``.

See journal: .context/implementation-notes-mollybots-port.md

Revision ID: 5cd737097039
Revises: 7f077607dc61
Create Date: 2026-05-07 11:46:07.525344

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5cd737097039"
down_revision: str | Sequence[str] | None = "7f077607dc61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_VIDEO_AND_VIEWABILITY_COLUMNS = (
    "video_completions",
    "video_starts",
    "video_first_quartile",
    "video_midpoints",
    "video_third_quartile",
    "viewable_impressions",
    "measurable_impressions",
)


def upgrade() -> None:
    """Add ``media_buys.gam_order_id`` and create ``agent_gam_cache``."""
    # 1) gam_order_id on media_buys (the poller's "what to refresh" key).
    op.execute(
        """
        ALTER TABLE media_buys
        ADD COLUMN IF NOT EXISTS gam_order_id TEXT
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_media_buys_gam_order_id
        ON media_buys (tenant_id, gam_order_id)
        WHERE gam_order_id IS NOT NULL
        """
    )

    # 2) agent_gam_cache table — one row per (tenant, order).
    cols = ",\n            ".join(f"{c} BIGINT NOT NULL DEFAULT 0" for c in _VIDEO_AND_VIEWABILITY_COLUMNS)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS agent_gam_cache (
            tenant_id   VARCHAR(50) NOT NULL,
            order_id    TEXT NOT NULL,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            impressions BIGINT NOT NULL DEFAULT 0,
            clicks      BIGINT NOT NULL DEFAULT 0,
            spend       NUMERIC(15,4) NOT NULL DEFAULT 0,
            {cols},
            PRIMARY KEY (tenant_id, order_id),
            CONSTRAINT fk_agent_gam_cache_tenant
                FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_gam_cache_fetched_at
        ON agent_gam_cache (fetched_at)
        """
    )


def downgrade() -> None:
    """Remove cache table and order-id column."""
    op.execute("DROP INDEX IF EXISTS idx_agent_gam_cache_fetched_at")
    op.execute("DROP TABLE IF EXISTS agent_gam_cache")
    op.execute("DROP INDEX IF EXISTS idx_media_buys_gam_order_id")
    op.execute("ALTER TABLE media_buys DROP COLUMN IF EXISTS gam_order_id")
