"""merge mock-platform branch into main mergepoint

Reconciles the two heads:
- ``e0f450f098de`` (main mergepoint: user_role rename + triton drop)
- ``o6p7q8r9s0t1`` (drop tenants.house_domain — sibling line my branch
  was based on before main re-merged its tip)

Revision ID: d8e9f0a1b2c3
Revises: e0f450f098de, o6p7q8r9s0t1
Create Date: 2026-05-07 15:55:00.000000

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = ("e0f450f098de", "o6p7q8r9s0t1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Merge migration — no schema changes."""
    pass


def downgrade() -> None:
    """Merge migration — no schema changes to revert."""
    pass
