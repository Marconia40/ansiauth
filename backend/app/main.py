import logging
import os
from dotenv import load_dotenv

# Read LOG_FORMAT early — before any other imports — so the root logger is
# configured before uvicorn's dictConfig runs its own handler setup. force=True
# ensures our handler wins regardless of order. uvicorn's own loggers are
# unaffected because they set propagate=False.
load_dotenv()

_log_format = os.getenv("LOG_FORMAT", "text").lower()
if _log_format == "json":
    try:
        from pythonjsonlogger.jsonlogger import JsonFormatter as _JsonFormatter
    except ImportError:  # python-json-logger < 3
        from pythonjsonlogger import jsonlogger as _jl  # type: ignore[no-redef]
        _JsonFormatter = _jl.JsonFormatter  # type: ignore[assignment]
    _handler = logging.StreamHandler()
    _handler.setFormatter(_JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    ))
    logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        force=True,
    )

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import (
    ARTIFACT_RETENTION_DAYS,
    AUDIT_RETENTION_DAYS,
    CLEANUP_INTERVAL_HOURS,
    CORS_ORIGINS,
    DATABASE_URL,
    LOGIN_ATTEMPT_RETENTION_DAYS,
    METRICS_ENABLED,
    SSL_CERTFILE,
)
from app.core.exceptions import (
    ConflictError,
    DeviceExecutionError,
    NotFoundError,
    UnsupportedVendorError,
    ValidationError,
)
from app.schemas.error import ErrorResponse, make_error  # noqa: F401 — re-exported for OpenAPI
from app.db.session import init_db
import app.db.models  # noqa: F401 — registers models with Base.metadata

logger = logging.getLogger(__name__)

# Initialize the engine and session factory. Schema management is owned by
# Alembic — run `alembic upgrade head` before starting the app on a fresh DB.
init_db(DATABASE_URL)
logger.info("Database ready: %s", DATABASE_URL)

from app.api import audit, auth, device_groups, devices, group_jobs, health, jobs, ports, sites, users, vlans  # noqa: E402 (must follow DB init)
from app.services import audit_service, job_service, site_service, user_service  # noqa: E402
from app.schemas.user import UserCreate  # noqa: E402

job_service.mark_orphaned_jobs_failed()


def _bootstrap_admin() -> None:
    """Create the initial super-admin user from env vars if no super-admin exists in the DB."""
    from app.core.config import BOOTSTRAP_ADMIN_USER, BOOTSTRAP_ADMIN_PASSWORD
    from app.db.models import UserModel
    from app.db.session import get_session

    with get_session() as session:
        has_superadmin = session.query(UserModel).filter_by(role="super-admin", is_active=True).first() is not None

    if has_superadmin:
        logger.info("Bootstrap skipped: active super-admin already exists")
        return

    if not BOOTSTRAP_ADMIN_PASSWORD:
        logger.warning(
            "No active super-admin exists and BOOTSTRAP_ADMIN_PASSWORD is not set — "
            "set this env var to seed an initial super-admin on first boot."
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
            role="super-admin",
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
    logger.info("Bootstrap: created super-admin user '%s' (id=%d)", user.username, user.id)


_bootstrap_admin()
# MSP: Phase 1 — idempotent bootstrap of the mandatory Base-Infrastructure
# Site + its Default DeviceGroup. Safe to call every boot.
site_service.ensure_base_infrastructure()


def _make_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    from app.services import audit_service as _audit
    from app.services import cleanup_service as _cleanup

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: _audit.purge_old_records(AUDIT_RETENTION_DAYS, triggered_by="scheduler"),
        trigger="cron",
        hour=2,
        minute=0,
        id="audit_purge_daily",
    )
    scheduler.add_job(
        lambda: _cleanup.run_all(
            artifact_retention_days=ARTIFACT_RETENTION_DAYS,
            login_attempt_retention_days=LOGIN_ATTEMPT_RETENTION_DAYS,
        ),
        trigger="interval",
        hours=CLEANUP_INTERVAL_HOURS,
        id="cleanup_sweep_interval",
    )
    return scheduler


@asynccontextmanager
async def _lifespan(app: FastAPI):
    from app.services import cleanup_service as _cleanup

    # Single startup pass so a long-running deployment doesn't have to wait a
    # full interval before unbounded tables are pruned for the first time.
    _cleanup.run_all(
        artifact_retention_days=ARTIFACT_RETENTION_DAYS,
        login_attempt_retention_days=LOGIN_ATTEMPT_RETENTION_DAYS,
    )

    scheduler = _make_scheduler()
    scheduler.start()
    logger.info(
        "Schedulers started: audit_purge_daily (retention=%d days, 02:00 UTC); "
        "cleanup_sweep_interval (every %d h, artifacts=%d days, login_attempts=%d days)",
        AUDIT_RETENTION_DAYS,
        CLEANUP_INTERVAL_HOURS,
        ARTIFACT_RETENTION_DAYS,
        LOGIN_ATTEMPT_RETENTION_DAYS,
    )
    yield
    scheduler.shutdown(wait=False)
    logger.info("Schedulers stopped")


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
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)

if METRICS_ENABLED:
    from prometheus_fastapi_instrumentator import Instrumentator  # noqa: E402
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")


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
