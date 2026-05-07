"""Agent Media Buys blueprint — read-only views over ``agent_gam_cache``.

Adapted from mollybots/salesagent-voxmedia ``src/admin/blueprints/
agent_media_buys.py`` (70 KB upstream). This port is intentionally
slimmer: list + detail with VCR + quartile breakdowns. Anything more
complex (charting, daily breakdown, cross-tenant rollups) can come in a
follow-on session.

**Feature-flagged.** Routes return 404 unless
``tenants.agent_media_buys_enabled`` is true AND
``SALESAGENT_FF_AGENT_CACHE`` is true. Both conditions are enforced via
``src/core/feature_flags.is_agent_media_buys_enabled``.

**Cache contract.** Read-only — never writes to ``agent_gam_cache`` (the
poller does that). Treats absence of a cache row as "no data yet" and
renders zeros / dashes accordingly. The backing query JOINs
``media_buys`` to ``agent_gam_cache`` on ``(tenant_id, gam_order_id)`` so
buys without a populated ``gam_order_id`` show "—" everywhere.

**AdCP impact.** The data this UI shows is the same data MCP
``get_media_buy_delivery`` already exposes (after the cache plumb-up):
``video_completions`` from cache populates the AdCP-spec
``DeliveryTotals.video_completions``, and the VCR shown in the table is
``video_completions / video_starts`` — a derived ratio not in the spec
but standard industry math.

See plan: ~/.claude/plans/yes-add-to-bead-logical-corbato.md
See journal: .context/implementation-notes-mollybots-port.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from flask import Blueprint, abort, render_template
from sqlalchemy import select, text

from src.admin.utils import require_tenant_access
from src.core.database.database_session import get_db_session
from src.core.database.models import MediaBuy, Tenant
from src.core.feature_flags import is_agent_media_buys_enabled

logger = logging.getLogger(__name__)

agent_media_buys_bp = Blueprint("agent_media_buys", __name__)


# ── Row dataclass — what the templates render ───────────────────────────────


@dataclass(frozen=True)
class AgentBuyRow:
    """One row in the Agent Media Buys list / detail.

    Combines media_buys metadata with agent_gam_cache totals. Cache fields
    are zero when no row exists (matching the BIGINT NOT NULL DEFAULT 0
    in the cache schema; missing rows are treated as zero rather than
    null because that's what the UI would show anyway).
    """

    media_buy_id: str
    order_name: str
    advertiser_name: str
    status: str
    start_date: Any
    end_date: Any
    budget: float | None
    currency: str
    gam_order_id: str | None

    # Cache columns — zero when no cache row yet
    impressions: int
    clicks: int
    spend: float
    video_starts: int
    video_completions: int
    video_first_quartile: int
    video_midpoints: int
    video_third_quartile: int
    viewable_impressions: int
    measurable_impressions: int
    fetched_at: datetime | None

    # Derived (computed in the template would be fine too, but easier to
    # test here)
    @property
    def vcr(self) -> float | None:
        """Video Completion Rate as a percentage 0-100, or None if no
        starts (avoid division by zero)."""
        if not self.video_starts:
            return None
        return 100.0 * self.video_completions / self.video_starts

    @property
    def viewability_pct(self) -> float | None:
        if not self.measurable_impressions:
            return None
        return 100.0 * self.viewable_impressions / self.measurable_impressions

    @property
    def ctr_pct(self) -> float | None:
        if not self.impressions:
            return None
        return 100.0 * self.clicks / self.impressions


# ── Internals ───────────────────────────────────────────────────────────────


_LIST_QUERY = text(
    """
    SELECT
        mb.media_buy_id,
        mb.order_name,
        mb.advertiser_name,
        mb.status,
        mb.start_date,
        mb.end_date,
        mb.budget,
        mb.currency,
        mb.gam_order_id,
        COALESCE(c.impressions, 0)            AS impressions,
        COALESCE(c.clicks, 0)                 AS clicks,
        COALESCE(c.spend, 0)::float           AS spend,
        COALESCE(c.video_starts, 0)           AS video_starts,
        COALESCE(c.video_completions, 0)      AS video_completions,
        COALESCE(c.video_first_quartile, 0)   AS video_first_quartile,
        COALESCE(c.video_midpoints, 0)        AS video_midpoints,
        COALESCE(c.video_third_quartile, 0)   AS video_third_quartile,
        COALESCE(c.viewable_impressions, 0)   AS viewable_impressions,
        COALESCE(c.measurable_impressions, 0) AS measurable_impressions,
        c.fetched_at
    FROM media_buys mb
    LEFT JOIN agent_gam_cache c
        ON c.tenant_id = mb.tenant_id
       AND c.order_id  = mb.gam_order_id
    WHERE mb.tenant_id = :tid
    ORDER BY mb.created_at DESC
    """
)


_DETAIL_QUERY = text(
    """
    SELECT
        mb.media_buy_id,
        mb.order_name,
        mb.advertiser_name,
        mb.status,
        mb.start_date,
        mb.end_date,
        mb.budget,
        mb.currency,
        mb.gam_order_id,
        COALESCE(c.impressions, 0)            AS impressions,
        COALESCE(c.clicks, 0)                 AS clicks,
        COALESCE(c.spend, 0)::float           AS spend,
        COALESCE(c.video_starts, 0)           AS video_starts,
        COALESCE(c.video_completions, 0)      AS video_completions,
        COALESCE(c.video_first_quartile, 0)   AS video_first_quartile,
        COALESCE(c.video_midpoints, 0)        AS video_midpoints,
        COALESCE(c.video_third_quartile, 0)   AS video_third_quartile,
        COALESCE(c.viewable_impressions, 0)   AS viewable_impressions,
        COALESCE(c.measurable_impressions, 0) AS measurable_impressions,
        c.fetched_at
    FROM media_buys mb
    LEFT JOIN agent_gam_cache c
        ON c.tenant_id = mb.tenant_id
       AND c.order_id  = mb.gam_order_id
    WHERE mb.tenant_id = :tid AND mb.media_buy_id = :mbid
    """
)


def _row_to_dataclass(row: Any) -> AgentBuyRow:
    """Translate a DB Row into the dataclass the templates expect."""
    return AgentBuyRow(
        media_buy_id=row.media_buy_id,
        order_name=row.order_name,
        advertiser_name=row.advertiser_name,
        status=row.status,
        start_date=row.start_date,
        end_date=row.end_date,
        budget=float(row.budget) if row.budget is not None else None,
        currency=row.currency or "USD",
        gam_order_id=row.gam_order_id,
        impressions=int(row.impressions),
        clicks=int(row.clicks),
        spend=float(row.spend),
        video_starts=int(row.video_starts),
        video_completions=int(row.video_completions),
        video_first_quartile=int(row.video_first_quartile),
        video_midpoints=int(row.video_midpoints),
        video_third_quartile=int(row.video_third_quartile),
        viewable_impressions=int(row.viewable_impressions),
        measurable_impressions=int(row.measurable_impressions),
        fetched_at=row.fetched_at,
    )


def _require_feature_or_404(tenant: Tenant | None) -> None:
    """Defense in depth: every route in this blueprint checks the flag."""
    if not is_agent_media_buys_enabled(tenant):
        abort(404)


# ── Routes ──────────────────────────────────────────────────────────────────


@agent_media_buys_bp.route("/<tenant_id>/agent-media-buys/")
@require_tenant_access()
def agent_media_buys_list(tenant_id: str):
    """List page — one row per media_buy with cache-derived metrics."""
    with get_db_session() as session:
        tenant = session.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
        _require_feature_or_404(tenant)
        rows = [_row_to_dataclass(r) for r in session.execute(_LIST_QUERY, {"tid": tenant_id}).fetchall()]
    return render_template(
        "agent_media_buy_list.html",
        tenant=tenant,
        tenant_id=tenant_id,
        buys=rows,
    )


@agent_media_buys_bp.route("/<tenant_id>/agent-media-buys/<media_buy_id>")
@require_tenant_access()
def agent_media_buy_detail(tenant_id: str, media_buy_id: str):
    """Detail page — single row, fuller breakdown."""
    with get_db_session() as session:
        tenant = session.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
        _require_feature_or_404(tenant)
        result = session.execute(_DETAIL_QUERY, {"tid": tenant_id, "mbid": media_buy_id}).first()
        if not result:
            # Fall back to the basic media_buys check so we 404 cleanly when
            # the buy doesn't exist at all.
            buy = session.scalars(select(MediaBuy).filter_by(tenant_id=tenant_id, media_buy_id=media_buy_id)).first()
            if not buy:
                abort(404)
            # Buy exists but somehow not in our LEFT JOIN — defensive 404.
            abort(404)
        row = _row_to_dataclass(result)
    return render_template(
        "agent_media_buy_detail.html",
        tenant=tenant,
        tenant_id=tenant_id,
        buy=row,
    )
