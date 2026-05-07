"""add modern ux feature flag

Adds ``tenants.modern_ux_enabled`` (default false). When on:
- Sales Agent logo + workspace name in the top bar are clickable home
  links (currently non-interactive).
- A persistent tenant-scoped secondary nav appears on every tenant page
  (Dashboard, Media Buys, Creatives, Products, Workflows, Settings).
- A global ``window.saToast()`` helper is available; AJAX/fetch actions
  show in-flight state + success/error toasts so saves are never
  silent.
- Inline action buttons (creative approve/reject, forecast refresh,
  flag-saving forms, etc.) get fetch-action wrapping so the user always
  has feedback.

Disabled state == today's UI byte-for-byte.

See journal: .context/implementation-notes-mollybots-port.md

Revision ID: cc000128065d
Revises: c409a0075fc7
Create Date: 2026-05-07 13:43:54.700884

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "cc000128065d"
down_revision: str | Sequence[str] | None = "c409a0075fc7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "modern_ux_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "modern_ux_enabled")
