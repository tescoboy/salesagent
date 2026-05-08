"""GAM line item repository — tenant-scoped reads for `gam_line_items` rows.

Used by the video-metric source classifier in
``src/adapters/gam_reporting_service.py`` to determine which GAM
completion-rate column to query (VAST vs viewership) based on the
synced line-item environment type and creative shape.

Core invariant: every query includes ``tenant_id`` in the WHERE clause.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.core.database.models import GAMLineItem


class GAMLineItemRepository:
    """Tenant-scoped data access for the synced ``gam_line_items`` table.

    Args:
        session: SQLAlchemy session (caller manages lifecycle).
        tenant_id: Tenant scope for all queries.
    """

    def __init__(self, session: Session, tenant_id: str) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def get_for_scope(
        self,
        order_id: str | None = None,
        line_item_id: str | None = None,
    ) -> list[GAMLineItem]:
        """Return synced line items matching the given GAM scope.

        Either ``order_id``, ``line_item_id``, or both may be supplied. With
        neither, returns an empty list (callers should default-handle missing
        scope at their layer).

        Args:
            order_id: GAM order ID. When set, returns every synced line item
                under the order.
            line_item_id: GAM line item ID. When set, returns the specific
                line item only.

        Returns:
            List of ``GAMLineItem`` rows scoped to this repository's tenant.
            Empty list when nothing matches (including the unscoped case).
        """
        if not order_id and not line_item_id:
            return []

        clauses = []
        if order_id is not None:
            clauses.append(GAMLineItem.order_id == str(order_id))
        if line_item_id is not None:
            clauses.append(GAMLineItem.line_item_id == str(line_item_id))

        stmt = select(GAMLineItem).where(
            GAMLineItem.tenant_id == self._tenant_id,
            or_(*clauses),
        )
        return list(self._session.scalars(stmt).all())
