import logging
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal = None


def init_db(database_url: str) -> None:
    global _engine, _SessionLocal
    logger.info("Initializing database: %s", database_url)
    # ``check_same_thread`` is a SQLite-only DB-API option — passing it to
    # any other driver (psycopg, mysqlclient, …) raises TypeError. Gate on
    # the URL prefix so the same init_db works for both backends.
    connect_args: dict = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    _engine = create_engine(database_url, connect_args=connect_args)
    # MSP: Phase 4 — turn on FK enforcement for SQLite so RESTRICT/CASCADE
    # rules from Alembic (esp. M3's device_groups.site_id → sites RESTRICT)
    # are actually applied. Postgres enforces FKs by default; SQLite doesn't
    # unless ``PRAGMA foreign_keys=ON`` fires on every new connection.
    if database_url.startswith("sqlite"):
        from sqlalchemy import event
        @event.listens_for(_engine, "connect")
        def _sqlite_enable_fk(dbapi_conn, _conn_record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    # Install the application-layer audit immutability guard now that the
    # Session class exists. Lazy import keeps the model graph out of the
    # session module's import-time dependency set.
    from app.db.audit_guard import install as _install_audit_guard
    _install_audit_guard()


def get_engine():
    if _engine is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _engine


@contextmanager
def get_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
