"""add tenant feature flag columns for mollybots port

Adds four boolean columns to ``tenants`` that gate the mollybots-port
features (per-tenant tier of the two-tier feature-flag system; the
global tier lives in ``SALESAGENT_FF_*`` env vars). All default to
``false`` so disabled state == today's behavior byte-for-byte.

See plan: ~/.claude/plans/yes-add-to-bead-logical-corbato.md
See journal: .context/implementation-notes-mollybots-port.md

Revision ID: 7f077607dc61
Revises: 3a085858fafb
Create Date: 2026-05-07 11:20:56.555941

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7f077607dc61"
down_revision: str | Sequence[str] | None = "3a085858fafb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_FLAG_COLUMNS = [
    "agent_media_buys_enabled",
    "product_forecast_enabled",
    "inventory_unified_enabled",
    "media_buy_approval_page_enabled",
]


def upgrade() -> None:
    """Add four boolean feature-flag columns to ``tenants``.

    Each column is ``NOT NULL`` with server default ``false`` so existing
    rows are populated atomically and disabled-state matches today's
    behavior. No data migration is needed.
    """
    for col in _FLAG_COLUMNS:
        op.add_column(
            "tenants",
            sa.Column(
                col,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    """Drop the four feature-flag columns."""
    for col in reversed(_FLAG_COLUMNS):
        op.drop_column("tenants", col)
