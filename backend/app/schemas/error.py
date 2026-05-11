from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

_STATUS_TO_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMIT_EXCEEDED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    details: Any | None = None
    timestamp: str


def make_error(
    status_code: int,
    message: str,
    error_code: str | None = None,
    details: Any = None,
) -> dict:
    return {
        "error_code": error_code or _STATUS_TO_CODE.get(status_code, "ERROR"),
        "message": message,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
