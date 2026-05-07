"""merge AAO + reporting_capabilities migration heads

Revision ID: p7q8r9s0t1u2
Revises: o6p7q8r9s0t1, 102ce62707b9
Create Date: 2026-05-07 16:00:00.000000

PR #78 (AAO Publisher Partnerships UI) and PR #110 (reporting_capabilities
non-null fix) both branched from ``e9a1c2d3f4b5`` and landed on main as
two separate heads. This empty merge migration unifies them so
``alembic upgrade head`` can resolve to a single revision.
"""

from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision: str = "p7q8r9s0t1u2"
down_revision: Union[str, Sequence[str], None] = ("o6p7q8r9s0t1", "102ce62707b9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op merge."""
    pass


def downgrade() -> None:
    """No-op merge."""
    pass
