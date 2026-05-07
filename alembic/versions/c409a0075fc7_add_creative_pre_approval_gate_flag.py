"""add creative pre approval gate flag

Adds the per-tenant ``creative_pre_approval_gate_enabled`` feature flag.
When enabled, creatives that arrive inline with ``create_media_buy``
(or via ``sync_creatives``) are NOT pushed to the ad server during
buy approval. They land locally at ``status='pending_review'`` and the
adapter's creative upload + line-item-creative-association only fires
when a publisher human (or AI auto-review path) flips the local status
to ``approved``.

Default ``false`` — disabled-state == today's execute-then-gate behaviour.

See journal: .context/implementation-notes-mollybots-port.md

Revision ID: c409a0075fc7
Revises: 5cd737097039
Create Date: 2026-05-07 12:48:07.739525

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c409a0075fc7"
down_revision: str | Sequence[str] | None = "5cd737097039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "creative_pre_approval_gate_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "creative_pre_approval_gate_enabled")
