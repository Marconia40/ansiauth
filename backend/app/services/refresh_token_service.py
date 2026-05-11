import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import REFRESH_TOKEN_EXPIRE_DAYS
from app.db.models import RefreshTokenModel
from app.db.session import get_session


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create(username: str) -> str:
    """Generate a new refresh token, store its hash, and return the raw token."""
    raw = secrets.token_urlsafe(32)
    with get_session() as session:
        session.add(
            RefreshTokenModel(
                token_hash=_hash(raw),
                username=username.lower(),
                expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
                revoked=False,
                created_at=datetime.now(timezone.utc),
            )
        )
    return raw


def validate_and_rotate(raw: str) -> tuple[str, str]:
    """Validate a refresh token, revoke it atomically, and issue a replacement.

    On replay (already-revoked token), all sessions for that user are revoked
    to contain potential token theft.

    Returns (new_raw_token, username).
    Raises ValueError on invalid, expired, or replayed token.
    """
    token_hash = _hash(raw)
    now = datetime.now(timezone.utc)

    # Collect error outside the context manager so that raises never prevent
    # the session from committing (cascade revocations must be persisted).
    error: str | None = None
    new_raw: str | None = None
    username_out: str | None = None

    with get_session() as session:
        record = session.query(RefreshTokenModel).filter_by(token_hash=token_hash).first()

        if record is None:
            error = "Invalid refresh token"
        elif record.revoked:
            # Replay detected — revoke every active token for this user before erroring
            session.query(RefreshTokenModel).filter_by(
                username=record.username, revoked=False
            ).update({"revoked": True}, synchronize_session=False)
            error = "Refresh token already used"
        else:
            expires_at = record.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < now:
                error = "Refresh token expired"
            else:
                username_out = record.username
                record.revoked = True
                new_raw = secrets.token_urlsafe(32)
                session.add(
                    RefreshTokenModel(
                        token_hash=_hash(new_raw),
                        username=username_out,
                        expires_at=now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
                        revoked=False,
                        created_at=now,
                    )
                )

    if error:
        raise ValueError(error)

    return new_raw, username_out


def revoke(raw: str) -> bool:
    """Revoke a token (logout). Returns True if found and revoked, False if not found."""
    token_hash = _hash(raw)
    with get_session() as session:
        record = session.query(RefreshTokenModel).filter_by(
            token_hash=token_hash, revoked=False
        ).first()
        if record is None:
            return False
        record.revoked = True
    return True
