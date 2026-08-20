"""MSP Phase 1 — Alembic upgrade / downgrade roundtrip.

Runs Alembic in a **subprocess** against an isolated scratch SQLite DB so
that ``app.core.config``'s module-level ``DATABASE_URL`` (imported once by
the parent test session) does not interfere. Env vars are inherited fresh
by the subprocess.

Asserts every added column/table is present after ``upgrade``, and gone
after ``downgrade -1``.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa


REVISION = "e1msp1_additive"
PARENT_REVISION = "d8a5f2c1b630"

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _alembic(url: str, *args: str) -> None:
    """Invoke alembic in a subprocess with DATABASE_URL scoped to url."""
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    # JWT key required by app.core.config on import.
    env.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_not_for_production")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed: {result.stderr}\n{result.stdout}"
    )


@pytest.fixture
def scratch_db():
    fd, path = tempfile.mkstemp(prefix="msp_m1_roundtrip_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    if os.path.exists(path):
        os.remove(path)


def _column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _table_names(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_adds_expected_columns_and_table(scratch_db):
    url = scratch_db

    # 1. Bring the scratch DB to the revision *before* MSP.
    _alembic(url, "upgrade", PARENT_REVISION)

    engine = sa.create_engine(url)
    try:
        assert "role_assignments" not in _table_names(engine)
        assert "kind" not in _column_names(engine, "sites")
        assert "default_group_id" not in _column_names(engine, "sites")
        assert "is_default" not in _column_names(engine, "device_groups")
        assert "device_group_id" not in _column_names(engine, "devices")
        assert "is_system_admin" not in _column_names(engine, "users")
    finally:
        engine.dispose()

    # 2. Upgrade to MSP additive.
    _alembic(url, "upgrade", REVISION)

    engine = sa.create_engine(url)
    try:
        assert "role_assignments" in _table_names(engine)
        assert "kind" in _column_names(engine, "sites")
        assert "default_group_id" in _column_names(engine, "sites")
        assert "is_default" in _column_names(engine, "device_groups")
        assert "device_group_id" in _column_names(engine, "devices")
        assert "is_system_admin" in _column_names(engine, "users")

        ra_indexes = {
            ix["name"] for ix in sa.inspect(engine).get_indexes("role_assignments")
        }
        for ix in (
            "ix_role_assignments_user_id",
            "ix_role_assignments_site_id",
            "ix_role_assignments_device_group_id",
        ):
            assert ix in ra_indexes, f"missing index {ix}"
    finally:
        engine.dispose()


def test_downgrade_removes_all_additions(scratch_db):
    url = scratch_db

    _alembic(url, "upgrade", REVISION)
    _alembic(url, "downgrade", PARENT_REVISION)

    engine = sa.create_engine(url)
    try:
        assert "role_assignments" not in _table_names(engine)
        assert "kind" not in _column_names(engine, "sites")
        assert "default_group_id" not in _column_names(engine, "sites")
        assert "is_default" not in _column_names(engine, "device_groups")
        assert "device_group_id" not in _column_names(engine, "devices")
        assert "is_system_admin" not in _column_names(engine, "users")
    finally:
        engine.dispose()
