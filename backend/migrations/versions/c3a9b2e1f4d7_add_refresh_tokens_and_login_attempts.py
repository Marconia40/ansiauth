"""add_refresh_tokens_and_login_attempts

Revision ID: c3a9b2e1f4d7
Revises: 9a675eb5f358
Create Date: 2026-05-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a9b2e1f4d7'
down_revision: Union[str, Sequence[str], None] = '9a675eb5f358'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('token_hash', sa.String(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_refresh_tokens_token_hash'), ['token_hash'], unique=True)
        batch_op.create_index(batch_op.f('ix_refresh_tokens_username'), ['username'], unique=False)

    op.create_table(
        'login_attempts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('ip_address', sa.String(), nullable=False),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('succeeded', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('login_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_login_attempts_username'), ['username'], unique=False)
        batch_op.create_index(batch_op.f('ix_login_attempts_ip_address'), ['ip_address'], unique=False)
        batch_op.create_index(batch_op.f('ix_login_attempts_attempted_at'), ['attempted_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('login_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_login_attempts_attempted_at'))
        batch_op.drop_index(batch_op.f('ix_login_attempts_ip_address'))
        batch_op.drop_index(batch_op.f('ix_login_attempts_username'))

    op.drop_table('login_attempts')

    with op.batch_alter_table('refresh_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_refresh_tokens_username'))
        batch_op.drop_index(batch_op.f('ix_refresh_tokens_token_hash'))

    op.drop_table('refresh_tokens')
