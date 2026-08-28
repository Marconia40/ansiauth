"""Shared fixtures for MSP-migration tests.

Every MSP-migration test runs Alembic in a **subprocess** against a scratch
SQLite database. Reasons:

  * ``app.core.config``'s module-level ``DATABASE_URL`` is only read once
    per test session — running Alembic in-process would either point at the
    session DB or require monkey-patching config module state.
  * A subprocess isolates the migration's own transactional state from any
    engine the test creates for assertions.

Every helper here is deliberately dialect-agnostic (SQLite in tests,
Postgres in prod) so the same seeding code works in both.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa


REV_M0 = "d8a5f2c1b630"
REV_M1 = "e1msp1_additive"
REV_M2 = "e2msp2_backfill"

_BACKEND_DIR = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _BACKEND_DIR / "alembic.ini"


def _run_alembic(
    url: str, *args: str, expect_success: bool = True
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_not_for_production")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), *args],
        cwd=str(_BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    if expect_success:
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed:\n"
            f"stderr:\n{result.stderr}\n"
            f"stdout:\n{result.stdout}"
        )
    return result


def upgrade(url: str, revision: str) -> None:
    _run_alembic(url, "upgrade", revision)


def try_upgrade(url: str, revision: str) -> subprocess.CompletedProcess:
    """Attempt an upgrade without asserting success — for negative tests."""
    return _run_alembic(url, "upgrade", revision, expect_success=False)


@pytest.fixture
def msp_scratch_db():
    """A brand-new, empty SQLite file. Yields a ``sqlite:///<path>`` URL."""
    fd, path = tempfile.mkstemp(prefix="msp_m2_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def db_at_m1(msp_scratch_db):
    """Scratch DB upgraded to Phase 1 (msp_additive) — schema present, no data."""
    upgrade(msp_scratch_db, REV_M1)
    return msp_scratch_db


# ─────────────────────────────────────────────────────────────────────────────
# Seed helpers — raw SQL against a scratch DB. Every helper accepts a
# SQLAlchemy connection so callers control the transaction boundary.
# ─────────────────────────────────────────────────────────────────────────────

def seed_site(conn, *, name: str, kind: str = "REGULAR") -> int:
    conn.execute(sa.text(
        "INSERT INTO sites (name, kind, created_at, updated_at) "
        "VALUES (:name, :kind, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ).bindparams(name=name, kind=kind))
    return conn.execute(
        sa.text("SELECT id FROM sites WHERE name = :name").bindparams(name=name)
    ).scalar()


def seed_device_group(conn, *, name: str, site_id: int | None = None) -> int:
    conn.execute(sa.text(
        "INSERT INTO device_groups (name, site_id, is_default, created_at) "
        "VALUES (:name, :site_id, 0, CURRENT_TIMESTAMP)"
    ).bindparams(name=name, site_id=site_id))
    return conn.execute(
        sa.text("SELECT id FROM device_groups WHERE name = :name").bindparams(
            name=name
        )
    ).scalar()


def seed_device(
    conn,
    *,
    name: str,
    site_id: int | None = None,
    vendor: str = "cisco",
) -> None:
    conn.execute(sa.text(
        "INSERT INTO devices (name, host, vendor, username, encrypted_password, "
        "                     site_id, created_at) "
        "VALUES (:name, :host, :vendor, :user, :pw, :site_id, CURRENT_TIMESTAMP)"
    ).bindparams(
        name=name,
        host=f"{name}.example.local",
        vendor=vendor,
        user="admin",
        pw="stub",
        site_id=site_id,
    ))


def seed_group_member(conn, *, group_id: int, device_name: str) -> None:
    conn.execute(sa.text(
        "INSERT INTO device_group_members (group_id, device_name, created_at) "
        "VALUES (:g, :d, CURRENT_TIMESTAMP)"
    ).bindparams(g=group_id, d=device_name))


def seed_user(conn, *, username: str, role: str) -> int:
    conn.execute(sa.text(
        "INSERT INTO users (username, hashed_password, role, is_active, "
        "                   is_system_admin, created_at, updated_at) "
        "VALUES (:u, 'stub', :r, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ).bindparams(u=username, r=role))
    return conn.execute(
        sa.text("SELECT id FROM users WHERE username = :u").bindparams(u=username)
    ).scalar()


def seed_allowed_site(conn, *, user_id: int, site_id: int) -> None:
    conn.execute(sa.text(
        "INSERT INTO user_allowed_sites (user_id, site_id, created_at) "
        "VALUES (:u, :s, CURRENT_TIMESTAMP)"
    ).bindparams(u=user_id, s=site_id))


def engine_for(url: str) -> sa.engine.Engine:
    """Fresh engine — callers should ``dispose()`` when done, or wrap in a
    context manager. Kept as a helper so every test uses the same options."""
    return sa.create_engine(url, future=True)
