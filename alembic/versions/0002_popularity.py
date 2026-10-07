"""popularity

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('games', sa.Column('final_popularity', sa.Numeric(precision=5, scale=4), nullable=True))
    op.add_column('game_days', sa.Column('popularity', sa.Numeric(precision=5, scale=4), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('game_days', 'popularity')
    op.drop_column('games', 'final_popularity')
