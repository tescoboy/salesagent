"""merge auto_naming and audit_log_signing heads

Revision ID: 3a085858fafb
Revises: b0545900b6b1, j0k1l2m3n4o5
Create Date: 2026-05-06 16:29:28.659820

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a085858fafb'
down_revision: Union[str, Sequence[str], None] = ('b0545900b6b1', 'j0k1l2m3n4o5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
