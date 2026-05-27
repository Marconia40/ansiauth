import logging

# uvicorn's dictConfig only configures its own loggers and leaves the root
# logger at WARNING with no handlers, so all app INFO/DEBUG output is silently
# dropped when running via `uvicorn app.main:app`. Calling basicConfig here
# (after uvicorn has already run its dictConfig) adds a stderr handler at INFO
# to the root logger. uvicorn's own loggers are unaffected because they set
# propagate=False and have their own handlers.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    force=True,
)

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import AUDIT_RETENTION_DAYS, DATABASE_URL, SSL_CERTFILE
from app.core.exceptions import (
    ConflictError,
    DeviceExecutionError,
    NotFoundError,
    UnsupportedVendorError,
    ValidationError,
)
from app.schemas.error import ErrorResponse, make_error  # noqa: F401 — re-exported for OpenAPI
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


def _migrate_rollback_success(engine) -> None:
    """Add the rollback_success column to jobs if it was not present in an older DB."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "jobs" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("jobs")}
        if "rollback_success" not in existing_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN rollback_success BOOLEAN DEFAULT NULL"))
                conn.commit()
            logger.info("Migration applied: added 'rollback_success' column to jobs")

_migrate_rollback_success(get_engine())


def _migrate_audit_log_columns(engine) -> None:
    """Add columns to audit_logs that were introduced after the initial schema."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "audit_logs" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("audit_logs")}
    additions = []
    if "parent_audit_id" not in existing_cols:
        additions.append("ALTER TABLE audit_logs ADD COLUMN parent_audit_id INTEGER REFERENCES audit_logs(id)")
    if "request_id" not in existing_cols:
        additions.append("ALTER TABLE audit_logs ADD COLUMN request_id VARCHAR")
    if additions:
        with engine.connect() as conn:
            for stmt in additions:
                conn.execute(text(stmt))
            conn.commit()
        logger.info("Migration applied: added columns to audit_logs: %s", [s.split("ADD COLUMN ")[1].split()[0] for s in additions])

_migrate_audit_log_columns(get_engine())


def _migrate_group_job_id(engine) -> None:
    """Add the group_job_id column to jobs if it was not present in an older DB."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "jobs" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("jobs")}
        if "group_job_id" not in existing_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN group_job_id VARCHAR DEFAULT NULL"))
                conn.commit()
            logger.info("Migration applied: added 'group_job_id' column to jobs")

_migrate_group_job_id(get_engine())


def _migrate_device_site_id(engine) -> None:
    """Add the site_id column to devices if it was not present in an older DB."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "devices" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("devices")}
        if "site_id" not in existing_cols:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE devices ADD COLUMN site_id INTEGER REFERENCES sites(id)"))
                conn.commit()
            logger.info("Migration applied: added 'site_id' column to devices")

_migrate_device_site_id(get_engine())


def _backfill_user_allowed_sites(engine) -> None:
    """Ensure each non-admin user with no allowed_sites rows gets one row per existing site.

    Runs on every startup but is idempotent — never duplicates existing rows.
    Lets the policy switch be non-breaking for already-deployed databases.
    """
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    needed = {"users", "sites", "user_allowed_sites"}
    if not needed.issubset(set(inspector.get_table_names())):
        return
    with engine.connect() as conn:
        # Only seed users who have zero allowed_sites rows and aren't admins —
        # that way an admin who explicitly empties a user's set never gets it re-filled.
        result = conn.execute(text("""
            INSERT INTO user_allowed_sites (user_id, site_id, created_at)
            SELECT u.id, s.id, CURRENT_TIMESTAMP
              FROM users u
             CROSS JOIN sites s
             WHERE u.role NOT IN ('admin', 'super-admin')
               AND NOT EXISTS (SELECT 1 FROM user_allowed_sites x WHERE x.user_id = u.id)
        """))
        inserted = getattr(result, "rowcount", 0) or 0
        if inserted > 0:
            conn.commit()
            logger.info("Backfilled %d user_allowed_sites rows", inserted)

_backfill_user_allowed_sites(get_engine())


def _migrate_device_group_site_id(engine) -> None:
    """Add the site_id column to device_groups (idempotent) and backfill rows
    whose members all live in the same site. Mixed-site groups are left with
    site_id=NULL — admins must clean them up before non-admin RBAC will surface them.
    """
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(engine)
    if "device_groups" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("device_groups")}
    if "site_id" not in existing_cols:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE device_groups ADD COLUMN site_id INTEGER REFERENCES sites(id)"))
            conn.commit()
        logger.info("Migration applied: added 'site_id' column to device_groups")
    # Backfill only NULL-site groups whose members all share a single site.
    with engine.connect() as conn:
        result = conn.execute(text("""
            UPDATE device_groups
               SET site_id = (
                   SELECT MIN(d.site_id)
                     FROM device_group_members m
                     JOIN devices d ON d.name = m.device_name
                    WHERE m.group_id = device_groups.id
                      AND d.site_id IS NOT NULL
               )
             WHERE site_id IS NULL
               AND id IN (
                   SELECT m.group_id
                     FROM device_group_members m
                     JOIN devices d ON d.name = m.device_name
                    GROUP BY m.group_id
                   HAVING MIN(d.site_id) = MAX(d.site_id)
                      AND MIN(d.site_id) IS NOT NULL
               )
        """))
        affected = getattr(result, "rowcount", 0) or 0
        if affected > 0:
            conn.commit()
            logger.info("Backfilled site_id on %d device_groups", affected)

_migrate_device_group_site_id(get_engine())


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

from app.api import audit, auth, device_groups, devices, group_jobs, health, jobs, ports, sites, users, vlans  # noqa: E402 (must follow DB init)
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


def _make_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services import audit_service as _audit

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: _audit.purge_old_records(AUDIT_RETENTION_DAYS, triggered_by="scheduler"),
        trigger="cron",
        hour=2,
        minute=0,
        id="audit_purge_daily",
    )
    return scheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    scheduler = _make_scheduler()
    scheduler.start()
    logger.info("Audit retention scheduler started (retention=%d days, runs daily at 02:00 UTC)", AUDIT_RETENTION_DAYS)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Audit retention scheduler stopped")


app = FastAPI(
    title="AnsiAuth — Network Automation API",
    version="1.0.0",
    description=(
        "Ansible-powered network automation platform for VLAN lifecycle management, "
        "multi-device orchestration, and audit-compliant configuration changes on "
        "Cisco IOS switches.\n\n"
        "## Authentication\n"
        "All endpoints except `/health` require a **Bearer JWT** in the `Authorization` header.\n"
        "Obtain a token from `POST /api/v1/auth/login` and click **Authorize** above.\n\n"
        "## Role hierarchy\n"
        "| Role | Permissions |\n"
        "|------|-------------|\n"
        "| `observer` | Read-only (VLANs, devices, jobs) |\n"
        "| `operator` | observer + create/update VLANs |\n"
        "| `admin` | operator + delete VLANs, manage devices and users |\n"
        "| `super-admin` | admin + manage users, purge audit log |"
    ),
    contact={"name": "Network Operations"},
    lifespan=_lifespan,
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "JWT access token obtained from `POST /api/v1/auth/login`. "
                "Include as `Authorization: Bearer <token>`."
            ),
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    # Remove per-endpoint security overrides that reference the now-replaced
    # OAuth2PasswordBearer scheme — global BearerAuth must apply instead.
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.pop("security", None)
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

from app.core.rate_limit_middleware import RateLimitMiddleware  # noqa: E402
from app.core.tls_middleware import HSTSMiddleware, HTTPSRedirectMiddleware  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(RateLimitMiddleware)

if SSL_CERTFILE:
    app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(HSTSMiddleware)

# CORS must be outermost so preflight OPTIONS requests are handled before
# rate limiting or auth middleware can reject them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error_code" in detail:
        error_code = detail["error_code"]
        message = detail.get("message", str(detail))
        details = detail.get("details")
    else:
        error_code = None
        message = str(detail) if detail is not None else "An error occurred"
        details = None
    return JSONResponse(
        status_code=exc.status_code,
        content=make_error(exc.status_code, message, error_code, details),
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    import json as _json

    try:
        errors_data = _json.loads(_json.dumps(exc.errors(), default=str))
    except Exception:
        errors_data = str(exc)

    try:
        body_bytes = await request.body()
        try:
            body_data = _json.loads(body_bytes)
        except Exception:
            body_data = body_bytes.decode("utf-8", errors="replace") if body_bytes else None
    except Exception:
        body_data = None

    audit_service.log_action(
        user="anonymous",
        action="validation_error",
        resource="request",
        status="failure",
        details={"errors": errors_data, "body": body_data},
    )
    return JSONResponse(
        status_code=422,
        content=make_error(422, "Request validation failed", "VALIDATION_ERROR", {"errors": errors_data}),
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    logger.warning("Validation error: %s", str(exc))
    return JSONResponse(status_code=400, content=make_error(400, str(exc), "VALIDATION_ERROR"))


@app.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError):
    logger.warning("Not found: %s", str(exc))
    return JSONResponse(status_code=404, content=make_error(404, str(exc), "NOT_FOUND"))


@app.exception_handler(ConflictError)
async def conflict_error_handler(request: Request, exc: ConflictError):
    logger.warning("Conflict: %s", str(exc))
    return JSONResponse(status_code=409, content=make_error(409, str(exc), "CONFLICT"))


@app.exception_handler(DeviceExecutionError)
async def device_execution_error_handler(request: Request, exc: DeviceExecutionError):
    logger.error("Device execution error: %s", str(exc))
    return JSONResponse(status_code=500, content=make_error(500, str(exc), "DEVICE_EXECUTION_ERROR"))


@app.exception_handler(UnsupportedVendorError)
async def unsupported_vendor_error_handler(request: Request, exc: UnsupportedVendorError):
    # Detailed vendor / platform context goes to the log only.
    logger.info(
        "Unsupported vendor for %s: vendor=%s platform=%s",
        exc.operation, exc.vendor, exc.platform,
    )
    return JSONResponse(
        status_code=501,
        content=make_error(
            501,
            f"{exc.operation} is not yet supported for this vendor.",
            "VENDOR_NOT_SUPPORTED",
        ),
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error: %s", str(exc))
    return JSONResponse(status_code=500, content=make_error(500, "Internal error", "INTERNAL_ERROR"))


app.include_router(health.router)

_err = {s: {"model": ErrorResponse} for s in (400, 401, 403, 404, 409, 422, 429, 500)}

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"], responses=_err)
app.include_router(vlans.router, prefix="/api/v1/vlans", tags=["vlans"], responses=_err)
app.include_router(ports.router, prefix="/api/v1/ports", tags=["ports"], responses=_err)
app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"], responses=_err)
app.include_router(devices.router, prefix="/api/v1/devices", tags=["devices"], responses=_err)
app.include_router(device_groups.router, prefix="/api/v1/device-groups", tags=["device-groups"], responses=_err)
app.include_router(sites.router, prefix="/api/v1/sites", tags=["sites"], responses=_err)
app.include_router(audit.router, prefix="/api/v1/audit", tags=["audit"], responses=_err)
app.include_router(users.router, prefix="/api/v1/users", tags=["users"], responses=_err)
app.include_router(group_jobs.router, prefix="/api/v1/group-jobs", tags=["group-jobs"], responses=_err)


@app.get("/")
def read_root():
    return {"message": "Network Automation API running"}
