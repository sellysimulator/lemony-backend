"""game_days.cost_per_cup may be null

A day with no sales stores what the cups you could have made would have cost, or
null when there was nothing to make a cup from. Rows stored before keep their value.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'game_days',
        'cost_per_cup',
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema (a null becomes 0.00 first, so the column can be NOT NULL again)."""
    op.execute('UPDATE game_days SET cost_per_cup = 0 WHERE cost_per_cup IS NULL')
    op.alter_column(
        'game_days',
        'cost_per_cup',
        existing_type=sa.Numeric(precision=12, scale=2),
        nullable=False,
    )
