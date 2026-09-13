"""Centralised application settings.

Every value the app reads from the environment lives on the :class:`Settings`
class below. Module-level constants are re-exported afterwards so existing
``from app.core.config import FOO`` callsites keep working — adding a new
setting only requires adding a field and a re-export line.
"""
import os
from typing import Annotated, List, Optional

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Populate ``os.environ`` from ``.env`` so the handful of non-Settings env
# readers in the codebase (e.g. ``ansible_service`` for ANSIBLE_TIMEOUT) keep
# picking up dev overrides. BaseSettings has its own ``env_file=`` loader, but
# that one only feeds the Settings instance, not ``os.environ``.
load_dotenv()

# Repo-relative defaults computed from this file's location. Not env-driven —
# they describe the on-disk layout of the project and never change between
# environments.
_DB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "db"))
_ANSIBLE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ansible")
)


class Settings(BaseSettings):
    """All environment-driven configuration for the backend.

    Values are loaded once at import time. Override any field by exporting an
    env var with the same name (case-insensitive) before the app boots, or by
    listing the variable in ``backend/.env``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Secrets ───────────────────────────────────────────────────────────────
    FERNET_KEY: Optional[str] = None
    JWT_SECRET_KEY: Optional[str] = None

    # ── Token lifetimes ───────────────────────────────────────────────────────
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=20, ge=1)
    REFRESH_TOKEN_EXPIRE_MINUTES: int = Field(default=240, ge=1)

    # ── Session hardening ─────────────────────────────────────────────────────
    # Idle timeout enforced server-side on every /auth/refresh call. If a
    # refresh token is not rotated within this window, the endpoint returns
    # 401 with detail="idle_timeout" and revokes the token chain (but not
    # the user's other sessions). Complements the fixed TTL above with a
    # true "no activity → logout" signal that does not rely on the client.
    REFRESH_TOKEN_IDLE_MINUTES: int = Field(default=15, ge=1)
    # Absolute session lifetime: hard ceiling from the original login
    # regardless of refresh activity. When exceeded, /auth/refresh returns
    # 401 with detail="session_absolute_limit" and revokes the whole
    # session chain (all rotations that share the same session_id).
    SESSION_ABSOLUTE_MAX_HOURS: int = Field(default=2, ge=1)
    # Step-up re-authentication token TTL (PR #5). Kept in the same
    # Settings block so PR #1 lands the field once and later PRs don't
    # touch config.py again.
    ELEVATED_TOKEN_EXPIRE_MINUTES: int = Field(default=5, ge=1, le=15)

    # ── Execution mode ────────────────────────────────────────────────────────
    EXECUTION_MODE: str = "mock"  # "mock" | "real"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = f"sqlite:///{_DB_DIR}/app.db"

    # ── First-run bootstrap ───────────────────────────────────────────────────
    BOOTSTRAP_ADMIN_USER: str = "admin"
    BOOTSTRAP_ADMIN_PASSWORD: Optional[str] = None

    # ── Rate limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_PER_IP: int = Field(default=20, ge=1)
    RATE_LIMIT_PER_USER: int = Field(default=200, ge=1)
    RATE_LIMIT_LOGIN: int = Field(default=5, ge=1)

    # ── TLS termination at the app ────────────────────────────────────────────
    SSL_CERTFILE: Optional[str] = None
    SSL_KEYFILE: Optional[str] = None

    # ── Audit ─────────────────────────────────────────────────────────────────
    AUDIT_RETENTION_DAYS: int = Field(default=90, ge=1)

    # ── Background cleanup jobs ───────────────────────────────────────────────
    # How often the cleanup scheduler runs all three sweep/purge tasks
    # (artifacts, expired refresh tokens, old login attempts). The first run
    # also fires once at startup.
    CLEANUP_INTERVAL_HOURS: int = Field(default=6, ge=1)
    # Days of ansible-runner artifact directories to keep on disk.
    ARTIFACT_RETENTION_DAYS: int = Field(default=30, ge=1)
    # Days of login_attempts rows to keep (only affects historical rows; the
    # lockout window is governed by login_attempt_repository constants).
    LOGIN_ATTEMPT_RETENTION_DAYS: int = Field(default=7, ge=1)

    # ── Observability ─────────────────────────────────────────────────────────
    # Emit structured JSON log lines instead of plain text. Set LOG_FORMAT=json
    # to switch; any other value (or unset) keeps the human-readable format.
    LOG_FORMAT: str = "text"
    # Expose a Prometheus-compatible /metrics endpoint. Off by default so
    # production deployments opt in explicitly.
    METRICS_ENABLED: bool = False

    # ── Celery + Redis ─────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Accepts a JSON list (e.g. '["https://app.example.com"]') or a comma-
    # separated string (e.g. 'https://a.example.com,https://b.example.com').
    # ``NoDecode`` keeps pydantic-settings from trying to JSON-parse the env
    # var before our validator runs — the validator handles both shapes.
    CORS_ORIGINS: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # ── Cookies ───────────────────────────────────────────────────────────────
    # Defaults are production-safe: Secure cookies, SameSite=strict. Local dev
    # without TLS must explicitly set COOKIE_SECURE=false to receive the
    # refresh-token cookie.
    COOKIE_SECURE: bool = True
    COOKIE_SAMESITE: str = "strict"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_origins(cls, value):
        """Accept CORS origins as either a JSON list or a comma-separated string."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("COOKIE_SAMESITE")
    @classmethod
    def _validate_samesite(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"strict", "lax", "none"}:
            raise ValueError(
                f"COOKIE_SAMESITE must be one of 'strict', 'lax', 'none' "
                f"(got: {value!r})"
            )
        return normalized

    @field_validator("EXECUTION_MODE")
    @classmethod
    def _validate_execution_mode(cls, value: str) -> str:
        if value not in {"mock", "real"}:
            raise ValueError(
                f"EXECUTION_MODE must be 'mock' or 'real' (got: {value!r})"
            )
        return value


# Single source of truth. Instantiated at import time so any env-var problem
# fails the process loudly before requests start arriving. Pydantic's
# ValidationError is wrapped in RuntimeError so callers and tests can rely on
# a single exception type for "configuration is invalid".
try:
    settings = Settings()
except Exception as exc:  # ValidationError or any source-level parse error
    raise RuntimeError(f"Invalid configuration: {exc}") from exc


# ─── Backward-compatible re-exports ──────────────────────────────────────────
# All existing call sites import named constants from this module; preserving
# them avoids touching every importer when a new setting is added.

FERNET_KEY = settings.FERNET_KEY
JWT_SECRET_KEY = settings.JWT_SECRET_KEY
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_EXPIRE_MINUTES = settings.REFRESH_TOKEN_EXPIRE_MINUTES
REFRESH_TOKEN_IDLE_MINUTES = settings.REFRESH_TOKEN_IDLE_MINUTES
SESSION_ABSOLUTE_MAX_HOURS = settings.SESSION_ABSOLUTE_MAX_HOURS
ELEVATED_TOKEN_EXPIRE_MINUTES = settings.ELEVATED_TOKEN_EXPIRE_MINUTES
EXECUTION_MODE = settings.EXECUTION_MODE
DATABASE_URL = settings.DATABASE_URL
BOOTSTRAP_ADMIN_USER = settings.BOOTSTRAP_ADMIN_USER
BOOTSTRAP_ADMIN_PASSWORD = settings.BOOTSTRAP_ADMIN_PASSWORD
RATE_LIMIT_PER_IP = settings.RATE_LIMIT_PER_IP
RATE_LIMIT_PER_USER = settings.RATE_LIMIT_PER_USER
RATE_LIMIT_LOGIN = settings.RATE_LIMIT_LOGIN
SSL_CERTFILE = settings.SSL_CERTFILE
SSL_KEYFILE = settings.SSL_KEYFILE
AUDIT_RETENTION_DAYS = settings.AUDIT_RETENTION_DAYS
CLEANUP_INTERVAL_HOURS = settings.CLEANUP_INTERVAL_HOURS
ARTIFACT_RETENTION_DAYS = settings.ARTIFACT_RETENTION_DAYS
LOGIN_ATTEMPT_RETENTION_DAYS = settings.LOGIN_ATTEMPT_RETENTION_DAYS
CORS_ORIGINS = settings.CORS_ORIGINS
COOKIE_SECURE = settings.COOKIE_SECURE
COOKIE_SAMESITE = settings.COOKIE_SAMESITE
LOG_FORMAT = settings.LOG_FORMAT
METRICS_ENABLED = settings.METRICS_ENABLED
REDIS_URL = settings.REDIS_URL

ANSIBLE_BASE_PATH = _ANSIBLE_DIR
INVENTORY_PATH = os.path.join(_ANSIBLE_DIR, "inventory", "inventory.ini")
