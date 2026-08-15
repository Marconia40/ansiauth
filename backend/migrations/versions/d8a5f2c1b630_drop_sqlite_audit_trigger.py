"""drop_sqlite_audit_immutability_trigger

Removes the SQLite-only ``audit_log_immutable`` trigger introduced in
``c4f7e2a9b610``. The portable replacement lives in
``app.db.audit_guard`` and runs at the ORM layer regardless of dialect.

The downgrade path re-creates the trigger on SQLite so a rollback restores
the previous defence-in-depth posture without rolling back the app guard
(which lives in source, not in the DB).

Revision ID: d8a5f2c1b630
Revises: c4f7e2a9b610
Create Date: 2026-06-03 00:00:02.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd8a5f2c1b630'
down_revision: Union[str, Sequence[str], None] = 'c4f7e2a9b610'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS audit_log_immutable
BEFORE UPDATE ON audit_logs
BEGIN
    SELECT RAISE(ABORT, 'audit_logs rows are immutable — use append_audit_event() instead');
END
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_log_immutable")


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(_TRIGGER_SQL)
