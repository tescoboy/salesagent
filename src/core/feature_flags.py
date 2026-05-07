"""Feature-flag accessors for the mollybots-port features.

Single source of truth for every flag check. All call sites in the rest
of the codebase MUST go through this module — no scattered
``os.environ.get(...)`` or ``tenant.foo_enabled`` reads. Centralizing
keeps the flag matrix legible and lets us add a per-environment override
layer (e.g. SaaS-style flag service) without touching call sites.

Two tiers, mirroring existing conventions in this codebase:

- Global env-var flags (``SALESAGENT_FF_*``) — gate infrastructure work
  that runs whether or not any specific tenant has opted in (cron polls,
  background jobs, GAM API calls). Same shape as the existing
  ``ADCP_AUTH_TEST_MODE``, ``ADCP_TESTING``, ``MANAGED_INSTANCE``
  patterns (see src/admin/blueprints/auth.py:203, src/admin/utils/
  embedded_mode_auth.py:76).
- Per-tenant boolean columns on the ``tenants`` table — gate
  user-visible UI and per-tenant data writes. Same shape as the existing
  ``auto_naming_enabled`` and ``gam_preset_sync_enabled`` patterns (see
  src/core/database/models.py:108, 1537).

Both tiers default to ``false``. Disabled state must equal today's
behavior byte-for-byte — that's a hard constraint, verified by the
plan's "flags off → diff = 0" acceptance criterion.

See plan: ~/.claude/plans/yes-add-to-bead-logical-corbato.md
See journal: .context/implementation-notes-mollybots-port.md
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.database.models import Tenant

logger = logging.getLogger(__name__)


# ── Global env-var flags ────────────────────────────────────────────────────


def _env_truthy(name: str) -> bool:
    """Return True if env var *name* is set to a truthy value.

    Truthy means: any of "true", "1", "yes", "on" (case-insensitive).
    Anything else — including unset, empty string, or "false" — is
    treated as False. Matches the convention used elsewhere in this
    codebase for env-var booleans.
    """
    raw = os.environ.get(name, "")
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def is_agent_cache_enabled() -> bool:
    """Global gate for the GAM delivery cache subsystem.

    When False, ``src/services/gam_delivery_poller.py`` does not run,
    ``agent_gam_cache`` is not written to, and reads from the cache in
    ``src/core/tools/media_buy_delivery.py`` are skipped (the call sites
    fall back to ``video_completions=None`` exactly as today).

    Env var: ``SALESAGENT_FF_AGENT_CACHE``
    """
    return _env_truthy("SALESAGENT_FF_AGENT_CACHE")


def is_product_forecast_enabled_globally() -> bool:
    """Global gate for the GAM ``ForecastService`` integration.

    When False, the ``GAMForecastService`` Python class refuses to make
    GAM SOAP calls, the ``POST /tenant/<tid>/products/<pid>/forecast/
    refresh`` endpoint returns 404, and the **Refresh Forecast** button
    is hidden in templates.

    Env var: ``SALESAGENT_FF_PRODUCT_FORECAST``
    """
    return _env_truthy("SALESAGENT_FF_PRODUCT_FORECAST")


# ── Per-tenant flags ────────────────────────────────────────────────────────


def is_agent_media_buys_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate for the Agent Media Buys UI + cache reads.

    Composed with the global ``is_agent_cache_enabled()`` — both must be
    True for the agent UI to render and for delivery responses to carry
    cached video metrics. Disabled state hides the sidebar entry, makes
    the agent_media_buys routes return 404, and skips cache lookups in
    ``media_buy_delivery.py``.
    """
    if tenant is None:
        return False
    if not is_agent_cache_enabled():
        return False
    return bool(getattr(tenant, "agent_media_buys_enabled", False))


def is_product_forecast_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate for the product availability-forecast feature.

    Composed with ``is_product_forecast_enabled_globally()`` — both must
    be True for the **Refresh Forecast** button to appear and the
    refresh endpoint to be reachable. MCP ``get_products`` continues to
    carry the ``forecast`` field whenever ``products.forecast`` is
    populated (regardless of flag state), since dropping a populated
    spec field would be a regression — but population only happens when
    both flags are on.
    """
    if tenant is None:
        return False
    if not is_product_forecast_enabled_globally():
        return False
    return bool(getattr(tenant, "product_forecast_enabled", False))


def is_inventory_unified_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate to swap the four classic inventory pages for the
    unified ``inventory_unified.html`` page.

    No global env-var counterpart — the unified UI doesn't require any
    new infrastructure, just the additional template + route + sidebar
    link. Disabled tenants see the classic four-page UI exactly as
    today.
    """
    if tenant is None:
        return False
    return bool(getattr(tenant, "inventory_unified_enabled", False))


def is_media_buy_approval_page_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate to route the Workflows "Review & Approve" link
    to a dedicated ``media_buy_approval.html`` page instead of the
    inline approval banner on ``media_buy_detail.html``.

    No global env-var counterpart — pure UX swap. Disabled tenants see
    today's inline-banner flow.
    """
    if tenant is None:
        return False
    return bool(getattr(tenant, "media_buy_approval_page_enabled", False))


def is_modern_ux_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate: enable the modern admin-UX layer.

    When on:
    - Top-bar Sales Agent logo + workspace name are clickable links to
      the tenant dashboard (or operator home for super admins).
    - A persistent tenant-scoped secondary nav appears on every tenant
      page (Dashboard, Media Buys, Creatives, Products, Workflows,
      Settings) with active-page highlighting.
    - Global ``window.saToast(message, type)`` helper available for
      every page. Flash messages are auto-mirrored to toasts.
    - AJAX action buttons (creative approve/reject, forecast refresh,
      flag toggles, etc.) get fetch-action wrapping that shows
      in-flight state + success / error toast so saves are never silent.

    Disabled state == today's UI byte-for-byte. Pure UI layer; no
    behavior or wire-format changes.
    """
    if tenant is None:
        return False
    return bool(getattr(tenant, "modern_ux_enabled", False))


def is_creative_pre_approval_gate_enabled(tenant: Tenant | None) -> bool:
    """Per-tenant gate: creatives arriving inline with ``create_media_buy``
    (or via ``sync_creatives``) stay LOCAL at ``status='pending_review'``
    instead of being uploaded to the ad server at buy-approval time.

    The adapter creative upload + line-item-creative-association
    (LICA) only fire when the publisher (or AI auto-review path) flips
    the local status to ``approved``. Closes the execute-then-gate hole
    where creatives reached the ad server before any human had reviewed
    them for policy violations.

    No global env-var counterpart — flipping creative review semantics
    is a per-tenant policy decision, not deployment-wide infrastructure.
    Disabled state == today's behaviour (creatives upload immediately on
    buy approval; local ``pending_review`` flag is cosmetic).
    """
    if tenant is None:
        return False
    return bool(getattr(tenant, "creative_pre_approval_gate_enabled", False))


# ── Introspection helpers (for the Beta-features settings card) ─────────────


_TENANT_FLAG_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (
        "agent_media_buys_enabled",
        "Agent Media Buys",
        "Render the Agent Media Buys list and detail pages "
        "(read from agent_gam_cache; requires SALESAGENT_FF_AGENT_CACHE "
        "to also be enabled globally).",
    ),
    (
        "product_forecast_enabled",
        "Product Availability Forecast",
        "Show a 'Refresh Forecast' button on GAM products that calls "
        "GAM's ForecastService to populate products.forecast (also "
        "requires SALESAGENT_FF_PRODUCT_FORECAST globally).",
    ),
    (
        "inventory_unified_enabled",
        "Unified Inventory Page",
        "Replace the four classic inventory pages with a single tree-driven unified browser.",
    ),
    (
        "media_buy_approval_page_enabled",
        "Dedicated Approval Page",
        "Route 'Review & Approve' to a dedicated approval page instead "
        "of the inline banner on the media-buy detail page.",
    ),
    (
        "creative_pre_approval_gate_enabled",
        "Creative Pre-Approval Gate",
        "Hold creatives at local pending_review until a human approves; "
        "ad-server upload only fires AFTER local approval. Without this "
        "flag, creatives are uploaded to the ad server immediately on "
        "buy approval and the local review flag is cosmetic.",
    ),
    (
        "modern_ux_enabled",
        "Modern UX (clickable logo, persistent nav, toasts)",
        "Logo + workspace name in the top bar become home links. A "
        "persistent tenant nav appears on every tenant page. Action "
        "buttons (saves, approves, refreshes) show in-flight state + "
        "success / error toast notifications instead of saving "
        "silently.",
    ),
)


def tenant_flag_definitions() -> tuple[tuple[str, str, str], ...]:
    """Return ``(column_name, label, description)`` triples for every
    per-tenant feature flag.

    Used by the **Beta features** settings card to render checkboxes
    without hard-coding the list in the template. Order is the order
    flags appear in the UI.
    """
    return _TENANT_FLAG_DEFINITIONS
