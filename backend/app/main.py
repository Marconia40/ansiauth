import logging

from fastapi import FastAPI, Request
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

from app.api import audit, auth, devices, jobs, vlans  # noqa: E402 (must follow DB init)

app = FastAPI()


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


@app.get("/")
def read_root():
    return {"message": "Network Automation API running"}
