import concurrent.futures
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter()

APP_VERSION = "1.0.0"


def _ping_db() -> bool:
    from app.db.session import get_engine
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@router.get(
    "/health",
    tags=["health"],
    summary="Health check",
    description=(
        "Returns API and database liveness status. "
        "Returns 200 when healthy, 503 when the database is unreachable. "
        "No authentication required."
    ),
)
def health():
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_ping_db)
        try:
            db_ok = future.result(timeout=0.1)
        except concurrent.futures.TimeoutError:
            db_ok = False

    status = "ok" if db_ok else "down"
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": status,
            "db_status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "version": APP_VERSION,
        },
    )
