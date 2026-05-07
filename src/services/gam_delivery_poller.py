"""GAM delivery cache poller — Python equivalent of mollybots' agent/agent.ts.

Pulls cumulative impression / click / spend / video / viewability totals
per GAM Order from ``ReportService`` and upserts into ``agent_gam_cache``.
Runs out-of-band (cron) so per-pageload UI render can read from cache
without a synchronous GAM SOAP roundtrip.

**Design contract:** this poller NEVER raises out of ``poll_tenant`` /
``poll_all_tenants``. Per-tenant errors are logged and the loop continues
to the next tenant. Per-order errors leave the previous cache row intact
(the upsert is skipped). The cache is a soft cache — degrading to "stale
data" or "no data" is always safer than crashing the cron.

**Feature-flagged:** the public ``poll_all_tenants()`` short-circuits when
``SALESAGENT_FF_AGENT_CACHE`` is off (the global env var). Per-tenant
``tenants.agent_media_buys_enabled`` further gates which tenants are
polled. Both flags off == no GAM API calls, no DB writes.

**AdCP impact:** the cached video / quartile / viewability columns
populate AdCP-spec fields on ``DeliveryTotals`` and ``PackageDelivery``
(``video_completions``, ``quartile_data``). The wire shape is unchanged —
we're just filling in fields that today are hardcoded ``None``.

GAM ReportService refs:
- https://developers.google.com/ad-manager/api/reference/v202602/ReportService
- https://developers.google.com/ad-manager/api/reference/v202602/ReportService.Column
- https://developers.google.com/ad-manager/api/reference/v202602/ReportService.Dimension

See plan: ~/.claude/plans/yes-add-to-bead-logical-corbato.md
See journal: .context/implementation-notes-mollybots-port.md
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.database.database_session import get_db_session
from src.core.database.models import AdapterConfig, MediaBuy, Tenant
from src.core.feature_flags import is_agent_cache_enabled, is_agent_media_buys_enabled

logger = logging.getLogger(__name__)


# ── GAM report shape ────────────────────────────────────────────────────────


# Dimensions: ORDER_ID is what we key by. ORDER_NAME is informational.
_GAM_DIMENSIONS = ["ORDER_ID", "ORDER_NAME"]

# Columns mapped 1:1 to agent_gam_cache columns (excluding tenant_id, order_id,
# fetched_at). Order matters for the column-name → DB-column dict below.
_GAM_COLUMN_TO_DB = {
    "AD_SERVER_IMPRESSIONS": "impressions",
    "AD_SERVER_CLICKS": "clicks",
    "AD_SERVER_CPM_AND_CPC_REVENUE": "spend",  # in micros — divide by 1e6
    "AD_SERVER_VIDEO_STARTS": "video_starts",
    "AD_SERVER_VIDEO_COMPLETIONS": "video_completions",
    "AD_SERVER_VIDEO_FIRST_QUARTILE_VIEWS": "video_first_quartile",
    "AD_SERVER_VIDEO_MIDPOINTS": "video_midpoints",
    "AD_SERVER_VIDEO_THIRD_QUARTILE_VIEWS": "video_third_quartile",
    "AD_SERVER_ACTIVE_VIEW_VIEWABLE_IMPRESSIONS": "viewable_impressions",
    "AD_SERVER_ACTIVE_VIEW_MEASURABLE_IMPRESSIONS": "measurable_impressions",
}

# Window: last 30 days. Cumulative totals — we don't store daily breakdown.
_DEFAULT_WINDOW_DAYS = 30

# GAM report polling: max wait + poll cadence. Reports for a single
# advertiser's orders typically complete in < 60 seconds.
_REPORT_TIMEOUT_SECONDS = 180
_REPORT_POLL_SECONDS = 5

# Allowed download domains (security check on the report URL). GAM CSV
# exports come from ``storage.googleapis.com``; matches the existing
# allowlist in src/adapters/gam_reporting_service.py:30.
_ALLOWED_DOMAINS = (".google.com", ".googleapis.com", ".googleusercontent.com")


@dataclass(frozen=True)
class TenantPollResult:
    """Outcome summary for one tenant's poll cycle."""

    tenant_id: str
    orders_attempted: int
    orders_upserted: int
    error: str | None = None


# ── Public API ──────────────────────────────────────────────────────────────


def poll_all_tenants() -> list[TenantPollResult]:
    """Poll every tenant whose flags are on. Returns per-tenant summaries.

    No-op (returns empty list) when ``SALESAGENT_FF_AGENT_CACHE`` is off.
    """
    if not is_agent_cache_enabled():
        logger.info("[gam_delivery_poller] SALESAGENT_FF_AGENT_CACHE is off; skipping.")
        return []

    results: list[TenantPollResult] = []
    with get_db_session() as session:
        tenants = session.scalars(select(Tenant).filter_by(is_active=True)).all()
        eligible = [t for t in tenants if is_agent_media_buys_enabled(t)]
        logger.info(f"[gam_delivery_poller] {len(eligible)} of {len(tenants)} tenants have agent_media_buys_enabled.")

    for tenant in eligible:
        try:
            results.append(poll_tenant(tenant.tenant_id))
        except Exception as exc:  # design contract: never raise out of the poll loop
            logger.exception(f"[gam_delivery_poller] tenant {tenant.tenant_id} crashed: {exc}")
            results.append(
                TenantPollResult(
                    tenant_id=tenant.tenant_id,
                    orders_attempted=0,
                    orders_upserted=0,
                    error=str(exc),
                )
            )
    return results


def poll_tenant(tenant_id: str, *, days: int = _DEFAULT_WINDOW_DAYS) -> TenantPollResult:
    """Poll a single tenant. Returns a summary; logs + swallows errors.

    Uses one GAM ReportService run covering all the tenant's
    ``media_buys.gam_order_id`` values within ORDER_ID dimension. Cumulative
    totals over ``days``-day window (default 30).
    """
    with get_db_session() as session:
        tenant = session.scalars(select(Tenant).filter_by(tenant_id=tenant_id)).first()
        if not tenant:
            return TenantPollResult(tenant_id, 0, 0, "tenant not found")
        if not is_agent_media_buys_enabled(tenant):
            return TenantPollResult(tenant_id, 0, 0, "feature flag off")

        cfg = session.scalars(select(AdapterConfig).filter_by(tenant_id=tenant_id)).first()
        if not cfg or cfg.adapter_type != "google_ad_manager":
            return TenantPollResult(tenant_id, 0, 0, "tenant not on GAM adapter")
        if not getattr(cfg, "gam_service_account_json", None):
            return TenantPollResult(tenant_id, 0, 0, "GAM service account not configured")

        order_ids = _list_order_ids(session, tenant_id)
        if not order_ids:
            return TenantPollResult(tenant_id, 0, 0, "no media_buys with gam_order_id")

        sa_json = cfg.gam_service_account_json
        network_code = cfg.gam_network_code

    # Build + run report outside the DB session — GAM calls can take a while,
    # no point holding a connection.
    try:
        rows = _run_report_for_orders(sa_json, network_code, order_ids, days=days)
    except Exception as exc:
        logger.warning(f"[gam_delivery_poller] tenant {tenant_id} report failed: {exc}")
        return TenantPollResult(tenant_id, len(order_ids), 0, f"report failed: {exc}")

    # Upsert results.
    with get_db_session() as session:
        upserted = _upsert_rows(session, tenant_id, rows)
        session.commit()
        return TenantPollResult(tenant_id, len(order_ids), upserted)


# ── Internals ───────────────────────────────────────────────────────────────


def _list_order_ids(session: Session, tenant_id: str) -> list[str]:
    """Return distinct, non-null GAM order_ids for the tenant."""
    rows = session.scalars(
        select(MediaBuy.gam_order_id).filter(
            MediaBuy.tenant_id == tenant_id,
            MediaBuy.gam_order_id.is_not(None),
        )
    ).all()
    # Dedup, keep insertion order
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _run_report_for_orders(sa_json: str, network_code: str, order_ids: list[str], *, days: int) -> list[dict]:
    """Run a single GAM ReportService report and return parsed rows.

    Each row is a dict with keys ``order_id``, plus one entry per DB column
    derived from ``_GAM_COLUMN_TO_DB``. Spend is normalised from micros to
    decimal.
    """
    # Imports are local so that this module is importable without googleads
    # installed (e.g. for unit tests of the public surface that don't actually
    # poll GAM).
    from src.adapters.gam.client import GAMClientManager

    client_manager = GAMClientManager(
        {"service_account_json": sa_json, "network_code": network_code},
        network_code,
    )
    client = client_manager.get_client()
    report_service = client.GetService("ReportService")

    end_d = date.today()
    start_d = end_d - timedelta(days=days)

    # Filter to only our orders so the response is tight even on networks
    # with thousands of orders we don't care about.
    in_clause = ",".join(str(int(oid)) for oid in order_ids if str(oid).isdigit())
    if not in_clause:
        return []

    report_job = {
        "reportQuery": {
            "dimensions": _GAM_DIMENSIONS,
            "columns": list(_GAM_COLUMN_TO_DB.keys()),
            "dateRangeType": "CUSTOM_DATE",
            "startDate": {"year": start_d.year, "month": start_d.month, "day": start_d.day},
            "endDate": {"year": end_d.year, "month": end_d.month, "day": end_d.day},
            "statement": {"query": f"WHERE ORDER_ID IN ({in_clause})"},
        }
    }

    job_response = report_service.runReportJob(report_job)
    job_id = getattr(job_response, "id", None) or (
        job_response.get("id") if isinstance(job_response, dict) else job_response
    )
    logger.info(f"[gam_delivery_poller] started report job {job_id} for {len(order_ids)} orders")

    waited = 0
    while waited < _REPORT_TIMEOUT_SECONDS:
        status = report_service.getReportJobStatus(job_id)
        if status == "COMPLETED":
            break
        if status == "FAILED":
            raise RuntimeError(f"GAM report job {job_id} FAILED")
        time.sleep(_REPORT_POLL_SECONDS)
        waited += _REPORT_POLL_SECONDS
    else:
        raise RuntimeError(f"GAM report job {job_id} timed out after {_REPORT_TIMEOUT_SECONDS}s")

    download_url = report_service.getReportDownloadURL(job_id, "CSV_DUMP")
    if not _is_safe_download_url(download_url):
        raise RuntimeError(f"Refusing to download from non-Google host: {download_url!r}")

    return _parse_csv_response(download_url)


def _is_safe_download_url(url: str) -> bool:
    """Reject any URL whose host isn't a Google domain. Same defense the
    existing ``gam_reporting_service.py`` uses (line ~355)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return any(host.endswith(domain) for domain in _ALLOWED_DOMAINS)


def _parse_csv_response(download_url: str) -> list[dict]:
    """Download the CSV (gzipped per ``CSV_DUMP``), parse, and normalize.

    Returns a list of dicts keyed by ``order_id`` plus DB column names.
    """
    resp = requests.get(download_url, timeout=(10, 60), stream=False)
    resp.raise_for_status()
    raw = resp.content
    # CSV_DUMP is gzipped CSV.
    try:
        text = gzip.decompress(raw).decode("utf-8")
    except OSError:
        text = raw.decode("utf-8")

    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for row in reader:
        order_id = (row.get("Dimension.ORDER_ID") or "").strip()
        if not order_id:
            continue
        record: dict = {"order_id": str(order_id)}
        for gam_col, db_col in _GAM_COLUMN_TO_DB.items():
            raw_val = row.get(f"Column.{gam_col}", "0") or "0"
            try:
                num = int(raw_val)
            except (TypeError, ValueError):
                try:
                    num = int(float(raw_val))
                except (TypeError, ValueError):
                    num = 0
            if db_col == "spend":
                # GAM revenue is in micros (1 USD = 1_000_000). Convert to a
                # decimal-friendly float; the column is NUMERIC(15,4).
                record[db_col] = num / 1_000_000
            else:
                record[db_col] = num
        out.append(record)
    return out


def _upsert_rows(session: Session, tenant_id: str, rows: list[dict]) -> int:
    """Upsert each row into ``agent_gam_cache``.

    Uses raw SQL because the table is intentionally lightweight (no ORM
    model — see migration 5cd737097039 commentary). Returns count of rows
    written.
    """
    if not rows:
        return 0

    fetched_at = datetime.now(UTC)
    columns = ["tenant_id", "order_id", "fetched_at", *_GAM_COLUMN_TO_DB.values()]
    placeholders = ", ".join(f":{c}" for c in columns)
    update_assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c not in {"tenant_id", "order_id"})
    sql = (
        f"INSERT INTO agent_gam_cache ({', '.join(columns)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (tenant_id, order_id) DO UPDATE SET {update_assignments}"
    )

    from sqlalchemy import text as sql_text

    written = 0
    for row in rows:
        params = {
            "tenant_id": tenant_id,
            "order_id": row["order_id"],
            "fetched_at": fetched_at,
            **{c: row.get(c, 0) for c in _GAM_COLUMN_TO_DB.values()},
        }
        session.execute(sql_text(sql), params)
        written += 1
    return written
