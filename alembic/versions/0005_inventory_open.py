"""inventory_open on game_days

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INGREDIENTS = ('ice', 'sugar', 'lemons', 'cups')


def upgrade() -> None:
    """Upgrade schema."""
    for name in _INGREDIENTS:
        op.add_column('game_days', sa.Column(f'inventory_open_{name}', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    for name in reversed(_INGREDIENTS):
        op.drop_column('game_days', f'inventory_open_{name}')
