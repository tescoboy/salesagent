#!/usr/bin/env python3
"""Cron entrypoint for the GAM delivery cache poller.

Invoked by the repo's ``crontab`` (see entry below in this file's
documentation). Iterates active tenants whose feature flags are on and
upserts cumulative delivery totals into ``agent_gam_cache``.

Usage:

    python scripts/cron/run_gam_delivery_poller.py            # all eligible tenants
    python scripts/cron/run_gam_delivery_poller.py --tenant default --once

Suggested crontab entry (every 30 minutes — enough freshness for an admin
UI dashboard, well under GAM's per-network rate limits):

    */30 * * * * python /app/scripts/cron/run_gam_delivery_poller.py

When ``SALESAGENT_FF_AGENT_CACHE`` is unset/false the script no-ops (logs
and exits cleanly) so leaving the cron entry on a deployment that hasn't
opted in is harmless.

See journal: .context/implementation-notes-mollybots-port.md
"""

from __future__ import annotations

import argparse
import logging
import sys

# Configure logging early so the poller's own messages reach stdout when
# invoked by cron.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("gam_delivery_poller_cron")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GAM delivery cache poller.")
    parser.add_argument(
        "--tenant",
        default=None,
        help="Restrict to a single tenant_id (default: all eligible tenants).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit (default — kept for symmetry with future daemon mode).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Reporting window length in days (default 30).",
    )
    args = parser.parse_args()

    # Imported here so --help works even if the import path has issues.
    from src.services.gam_delivery_poller import (
        TenantPollResult,
        is_agent_cache_enabled,
        poll_all_tenants,
        poll_tenant,
    )

    if not is_agent_cache_enabled():
        logger.info("SALESAGENT_FF_AGENT_CACHE is off — exiting cleanly with no work.")
        return 0

    if args.tenant:
        results: list[TenantPollResult] = [poll_tenant(args.tenant, days=args.days)]
    else:
        results = poll_all_tenants()

    total_attempted = sum(r.orders_attempted for r in results)
    total_upserted = sum(r.orders_upserted for r in results)
    failures = [r for r in results if r.error]

    logger.info(
        "Poll complete: %d tenant(s), %d orders attempted, %d upserted, %d errors.",
        len(results),
        total_attempted,
        total_upserted,
        len(failures),
    )
    for r in failures:
        logger.warning("  tenant=%s error=%s", r.tenant_id, r.error)

    # Exit non-zero if every tenant failed (so cron emails the operator).
    # Partial failures are not exit-non-zero — soft cache failures are
    # expected on tenants that haven't completed their GAM setup yet.
    if results and len(failures) == len(results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
