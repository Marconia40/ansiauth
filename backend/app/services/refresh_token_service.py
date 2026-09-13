import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import (
    REFRESH_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_IDLE_MINUTES,
    SESSION_ABSOLUTE_MAX_HOURS,
)
from app.db.models import RefreshTokenModel
from app.db.session import get_session

# Sentinel strings raised by ``validate_and_rotate`` — stable so the auth
# endpoint can map them to the response ``detail`` codes the frontend keys
# off. Do NOT translate; the login page reads the code and picks the copy.
ERR_INVALID = "Invalid refresh token"
ERR_EXPIRED = "Refresh token expired"
ERR_REPLAY = "Refresh token already used"
ERR_IDLE = "idle_timeout"
ERR_ABSOLUTE = "session_absolute_limit"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    """SQLite drops the tzinfo when a row round-trips; treat naive timestamps
    as UTC (which is what ``create``/``validate_and_rotate`` always write)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def create(
    username: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    session_id: str | None = None,
    session_started_at: datetime | None = None,
    parent_id: int | None = None,
) -> str:
    """Generate a new refresh token, store its hash, and return the raw token.

    A fresh login must call this without ``session_id`` — a new UUID is
    minted and becomes the anchor of the session chain. Rotations pass the
    existing ``session_id`` and ``session_started_at`` so the whole chain
    shares the same absolute-lifetime start.
    """
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with get_session() as session:
        session.add(
            RefreshTokenModel(
                token_hash=_hash(raw),
                username=username.lower(),
                expires_at=now + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES),
                revoked=False,
                created_at=now,
                last_used_at=now,
                session_id=session_id or uuid.uuid4().hex,
                session_started_at=session_started_at or now,
                parent_id=parent_id,
                ip_address=ip_address,
                user_agent=(user_agent or None) and user_agent[:512],
            )
        )
    return raw


def validate_and_rotate(
    raw: str,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str, str]:
    """Validate a refresh token, revoke it atomically, and issue a replacement.

    Checks in order: invalid hash → replay (already revoked, cascade-revokes
    the whole session chain) → hard TTL → idle timeout (revokes only the
    current chain, not the user's other sessions) → absolute lifetime
    (cascade-revokes the chain).

    Returns ``(new_raw_token, username, session_id)``. Raises ``ValueError``
    with one of the ``ERR_*`` sentinels; ``api/auth.py`` maps those to the
    response ``detail`` code the frontend distinguishes.
    """
    token_hash = _hash(raw)
    now = datetime.now(timezone.utc)

    # Collect result outside the context manager so raising never prevents
    # the session from committing cascade revocations.
    error: str | None = None
    new_raw: str | None = None
    username_out: str | None = None
    session_id_out: str | None = None

    with get_session() as session:
        record = session.query(RefreshTokenModel).filter_by(token_hash=token_hash).first()

        if record is None:
            error = ERR_INVALID
        elif record.revoked:
            # Replay: revoke every active token for this user across all
            # sessions. Different from idle_timeout on purpose — a replay
            # is potentially a stolen token, an idle logout is not.
            session.query(RefreshTokenModel).filter_by(
                username=record.username, revoked=False
            ).update({"revoked": True}, synchronize_session=False)
            error = ERR_REPLAY
        elif _aware(record.expires_at) < now:
            error = ERR_EXPIRED
        elif now - _aware(record.last_used_at) > timedelta(minutes=REFRESH_TOKEN_IDLE_MINUTES):
            # Idle timeout: user walked away. Revoke just this chain so the
            # attacker who sits down at the machine cannot resume — but keep
            # the user's other sessions (phone, another laptop) alive.
            session.query(RefreshTokenModel).filter_by(
                session_id=record.session_id, revoked=False
            ).update({"revoked": True}, synchronize_session=False)
            error = ERR_IDLE
        elif now - _aware(record.session_started_at) > timedelta(hours=SESSION_ABSOLUTE_MAX_HOURS):
            # Absolute lifetime hit — revoke the whole session chain even if
            # the user is still active. This is the "force re-login on a
            # predictable cadence" guarantee.
            session.query(RefreshTokenModel).filter_by(
                session_id=record.session_id, revoked=False
            ).update({"revoked": True}, synchronize_session=False)
            error = ERR_ABSOLUTE
        else:
            username_out = record.username
            session_id_out = record.session_id
            session_started_at = _aware(record.session_started_at)
            parent_id = record.id
            record.revoked = True
            record.last_used_at = now
            new_raw = secrets.token_urlsafe(32)
            session.add(
                RefreshTokenModel(
                    token_hash=_hash(new_raw),
                    username=username_out,
                    expires_at=now + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES),
                    revoked=False,
                    created_at=now,
                    last_used_at=now,
                    session_id=session_id_out,
                    session_started_at=session_started_at,
                    parent_id=parent_id,
                    ip_address=ip_address,
                    user_agent=(user_agent or None) and user_agent[:512],
                )
            )

    if error:
        raise ValueError(error)

    return new_raw, username_out, session_id_out


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


def get_session_id_for_raw(raw: str) -> str | None:
    """Look up the ``session_id`` for the (possibly revoked) row that
    ``raw`` hashes to. Used by ``/auth/sessions`` to flag which row in the
    list belongs to the caller — never raises, returns None on miss."""
    token_hash = _hash(raw)
    with get_session() as session:
        record = session.query(RefreshTokenModel).filter_by(token_hash=token_hash).first()
        return record.session_id if record else None


def list_active_sessions(
    username: str, *, current_session_id: str | None = None
) -> list[dict]:
    """Return one entry per active session (grouped by ``session_id``) for
    ``username``, using the most recently used row of each chain as the
    representative. A session is "active" when it has at least one row
    that is not revoked and not past ``expires_at``.

    ``current_session_id`` — if provided, the row matching it is flagged
    with ``current=True`` so the UI can render the "this device" pill.
    """
    username = (username or "").lower()
    now = datetime.now(timezone.utc)
    # Materialise rows as plain dicts inside the session so callers can
    # freely read them after the connection closes (Detached* errors).
    with get_session() as session:
        rows = (
            session.query(
                RefreshTokenModel.session_id,
                RefreshTokenModel.session_started_at,
                RefreshTokenModel.last_used_at,
                RefreshTokenModel.expires_at,
                RefreshTokenModel.ip_address,
                RefreshTokenModel.user_agent,
            )
            .filter(RefreshTokenModel.username == username)
            .filter(RefreshTokenModel.revoked.is_(False))
            .filter(RefreshTokenModel.expires_at > now)
            .order_by(
                RefreshTokenModel.session_id,
                RefreshTokenModel.last_used_at.desc(),
            )
            .all()
        )

    # Take the first (highest last_used_at) row per session_id.
    seen: set[str] = set()
    out: list[dict] = []
    for sid, started_at, last_used, expires, ip, ua in rows:
        if sid in seen:
            continue
        seen.add(sid)
        out.append({
            "session_id": sid,
            "current": sid == current_session_id,
            "created_at": _aware(started_at),
            "last_used_at": _aware(last_used),
            "expires_at": _aware(expires),
            "ip_address": ip,
            "user_agent": ua,
        })
    # Newest activity first so the current tab tends to sort near the top.
    out.sort(key=lambda s: s["last_used_at"], reverse=True)
    return out


def revoke_other_sessions(username: str, keep_session_id: str) -> int:
    """Revoke every non-revoked row for ``username`` whose ``session_id``
    is not ``keep_session_id``. Returns the number of rows revoked (across
    all rotations of every other chain) so the caller can audit-log the
    magnitude of the action.
    """
    username = (username or "").lower()
    with get_session() as session:
        revoked = (
            session.query(RefreshTokenModel)
            .filter(RefreshTokenModel.username == username)
            .filter(RefreshTokenModel.revoked.is_(False))
            .filter(RefreshTokenModel.session_id != keep_session_id)
            .update({"revoked": True}, synchronize_session=False)
        )
    return int(revoked)
