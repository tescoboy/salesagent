"""merge role-rename with triton-merge heads

Revision ID: q8r9s0t1u2v3
Revises: 8407a32e9b07, p7q8r9s0t1u2
Create Date: 2026-05-07 17:00:00.000000

PR #112 (rename User.role manager → member, merge migration ``8407a32e9b07``)
and PR #127 (triton/freewheel rebuild, merge migration ``p7q8r9s0t1u2``)
both landed on main as independent merge migrations of the same upstream
heads (``102ce62707b9`` + ``o6p7q8r9s0t1``). Neither chains through the
other so ``alembic upgrade head`` sees two heads.

Empty no-op merge to converge them so ``upgrade head`` resolves cleanly.
"""

from collections.abc import Sequence

from alembic import op  # noqa: F401

revision: str = "q8r9s0t1u2v3"
down_revision: tuple[str, ...] | str | Sequence[str] | None = (
    "8407a32e9b07",
    "p7q8r9s0t1u2",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
