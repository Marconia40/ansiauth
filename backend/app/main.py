import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import DATABASE_URL
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.db.base import Base
from app.db.session import get_engine, init_db
import app.db.models  # noqa: F401 — registers models with Base.metadata

logger = logging.getLogger(__name__)

# Initialize database on module load so it is ready before any request.
init_db(DATABASE_URL)
Base.metadata.create_all(bind=get_engine())
logger.info("Database ready: %s", DATABASE_URL)


def _migrate_device_platform(engine) -> None:
    """Add the platform column to devices if it was not present in an older DB."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "devices" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("devices")}
        if "platform" not in existing_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE devices ADD COLUMN platform VARCHAR DEFAULT 'ios'"))
                conn.commit()
            logger.info("Migration applied: added 'platform' column to devices")

_migrate_device_platform(get_engine())


def _install_audit_immutability_trigger(engine) -> None:
    """Create a BEFORE UPDATE trigger that prevents any mutation of audit_logs rows."""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS audit_log_immutable
            BEFORE UPDATE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs rows are immutable — use append_audit_event() instead');
            END
        """))
        conn.commit()
    logger.info("Audit immutability trigger installed")

_install_audit_immutability_trigger(get_engine())

from app.api import audit, auth, devices, jobs, users, vlans  # noqa: E402 (must follow DB init)
from app.services import audit_service, job_service, user_service  # noqa: E402
from app.schemas.user import UserCreate  # noqa: E402

job_service.mark_orphaned_jobs_failed()


def _bootstrap_admin() -> None:
    """Create the initial admin user from env vars if no admin exists in the DB."""
    from app.core.config import BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_PASSWORD
    from app.db.models import UserModel
    from app.db.session import get_session

    with get_session() as session:
        has_admin = session.query(UserModel).filter_by(role="admin", is_active=True).first() is not None

    if has_admin:
        logger.info("Bootstrap skipped: active admin already exists")
        return

    if not BOOTSTRAP_ADMIN_PASSWORD:
        logger.warning(
            "No active admin exists and BOOTSTRAP_ADMIN_PASSWORD is not set — "
            "set this env var to seed an initial admin on first boot."
        )
        return

    if len(BOOTSTRAP_ADMIN_PASSWORD) < 12:
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_PASSWORD must be at least 12 characters"
        )

    user = user_service.create_user(
        UserCreate(
            username=BOOTSTRAP_ADMIN_USER,
            password=BOOTSTRAP_ADMIN_PASSWORD,
            role="admin",
        )
    )
    audit_service.log_action(
        user="system",
        action="bootstrap_admin",
        resource="user",
        resource_id=str(user.id),
        details={"username": user.username},
        status="success",
    )
    logger.info("Bootstrap: created admin user '%s' (id=%d)", user.username, user.id)


_bootstrap_admin()

app = FastAPI()


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    try:
        body_bytes = await request.body()
        try:
            import json
            body_data = json.loads(body_bytes)
        except Exception:
            body_data = body_bytes.decode("utf-8", errors="replace") if body_bytes else None
    except Exception:
        body_data = None

    audit_service.log_action(
        user="anonymous",
        action="validation_error",
        resource="request",
        status="failure",
        details={"errors": exc.errors(), "body": body_data},
    )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    logger.warning("Validation error: %s", str(exc))
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError):
    logger.warning("Not found: %s", str(exc))
    return JSONResponse(status_code=404, content={"error": str(exc)})


@app.exception_handler(DeviceExecutionError)
async def device_execution_error_handler(request: Request, exc: DeviceExecutionError):
    logger.error("Device execution error: %s", str(exc))
    return JSONResponse(status_code=500, content={"error": str(exc)})


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", str(exc))
    return JSONResponse(status_code=500, content={"error": "Internal error"})


app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(vlans.router, prefix="/api/v1/vlans", tags=["vlans"])
app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"])
app.include_router(devices.router, prefix="/api/v1/devices", tags=["devices"])
app.include_router(audit.router, prefix="/api/v1/audit", tags=["audit"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])


@app.get("/")
def read_root():
    return {"message": "Network Automation API running"}
