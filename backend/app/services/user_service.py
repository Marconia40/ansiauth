import logging
from datetime import datetime, timezone
from typing import Optional

from passlib.context import CryptContext

from app.db.models import UserModel
from app.db.session import get_session
from app.schemas.user import UserCreate, UserRead, UserUpdate

logger = logging.getLogger(__name__)

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

VALID_ROLES = frozenset({"super-admin", "admin", "operator", "observer"})


# ── Internal helpers ──────────────────────────────────────────────────────────

def _to_user_read(row: UserModel) -> UserRead:
    # Resolve allowed_sites while the session is still open. Admins are unrestricted
    # by policy, but we still return whatever rows happen to exist so the UI can
    # show their assignments without conditional rendering.
    allowed = list(row.allowed_sites or [])
    return UserRead(
        id=row.id,
        username=row.username,
        email=row.email,
        role=row.role,
        is_active=row.is_active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        allowed_site_ids=sorted(s.id for s in allowed),
        allowed_site_names=sorted(s.name for s in allowed),
    )


def _get_row_by_id(user_id: int, session) -> Optional[UserModel]:
    return session.query(UserModel).filter_by(id=user_id).first()


def _get_row_by_username(username: str, session) -> Optional[UserModel]:
    return session.query(UserModel).filter_by(username=username.lower()).first()


def _is_last_active_admin(user_id: int, session) -> bool:
    """Return True if this user is the only remaining active admin."""
    row = _get_row_by_id(user_id, session)
    if row is None or row.role != "admin":
        return False
    active_admin_count = (
        session.query(UserModel)
        .filter_by(role="admin", is_active=True)
        .count()
    )
    return active_admin_count <= 1


def _is_last_active_super_admin(user_id: int, session) -> bool:
    """Return True if this user is the only remaining active super-admin."""
    row = _get_row_by_id(user_id, session)
    if row is None or row.role != "super-admin":
        return False
    active_super_admin_count = (
        session.query(UserModel)
        .filter_by(role="super-admin", is_active=True)
        .count()
    )
    return active_super_admin_count <= 1


# ── Public API ────────────────────────────────────────────────────────────────

def create_user(data: UserCreate) -> UserRead:
    """Create a new user. Normalises username/email to lowercase and hashes the password."""
    username = data.username.lower()
    email = data.email.lower() if data.email else None

    # Validate every requested allowed_site_id up front so we don't half-create the user.
    site_ids = list(data.allowed_site_ids or [])
    if site_ids:
        from app.db.models import SiteModel
        with get_session() as session:
            existing = {
                r[0] for r in session.query(SiteModel.id).filter(SiteModel.id.in_(site_ids)).all()
            }
            missing = set(site_ids) - existing
            if missing:
                raise ValueError(f"Unknown site IDs: {sorted(missing)}")

    with get_session() as session:
        if _get_row_by_username(username, session):
            raise ValueError(f"Username '{username}' is already taken")
        if email:
            existing = session.query(UserModel).filter_by(email=email).first()
            if existing:
                raise ValueError(f"Email '{email}' is already registered")

        row = UserModel(
            username=username,
            email=email,
            hashed_password=_pwd_context.hash(data.password),
            role=data.role,
            is_active=True,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        if site_ids:
            from app.db.models import UserAllowedSiteModel
            for sid in sorted(set(site_ids)):
                session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))
            session.flush()
            session.refresh(row)
        result = _to_user_read(row)

    logger.info("User created: username=%s role=%s allowed_sites=%s", username, data.role, sorted(set(site_ids)))
    return result


def get_by_id(user_id: int) -> Optional[UserRead]:
    with get_session() as session:
        row = _get_row_by_id(user_id, session)
        return _to_user_read(row) if row else None


def get_by_username(username: str) -> Optional[UserRead]:
    with get_session() as session:
        row = _get_row_by_username(username, session)
        return _to_user_read(row) if row else None


def list_users(include_inactive: bool = False) -> list[UserRead]:
    with get_session() as session:
        q = session.query(UserModel)
        if not include_inactive:
            q = q.filter_by(is_active=True)
        rows = q.order_by(UserModel.created_at).all()
        return [_to_user_read(r) for r in rows]


def update_user(user_id: int, data: UserUpdate) -> UserRead:
    """Update mutable user fields. Deactivation goes through deactivate_user()."""
    # Distinguish "not provided" vs "explicitly set to []" for allowed_site_ids.
    explicit = data.model_dump(exclude_unset=True)
    has_allowed_sites_patch = "allowed_site_ids" in explicit

    with get_session() as session:
        row = _get_row_by_id(user_id, session)
        if row is None:
            raise ValueError(f"User {user_id} not found")

        if data.email is not None:
            email = data.email.lower()
            clash = session.query(UserModel).filter_by(email=email).first()
            if clash and clash.id != user_id:
                raise ValueError(f"Email '{email}' is already registered")
            row.email = email

        if data.role is not None:
            row.role = data.role

        if data.password is not None:
            row.hashed_password = _pwd_context.hash(data.password)

        if data.is_active is False:
            if _is_last_active_admin(user_id, session):
                raise ValueError("Cannot deactivate the last active admin account")
            if _is_last_active_super_admin(user_id, session):
                raise ValueError("Cannot deactivate the last active super-admin account")
            row.is_active = False
        elif data.is_active is True:
            row.is_active = True

        if has_allowed_sites_patch:
            from app.db.models import SiteModel, UserAllowedSiteModel
            requested = set(data.allowed_site_ids or [])
            if requested:
                existing = {
                    r[0]
                    for r in session.query(SiteModel.id).filter(SiteModel.id.in_(requested)).all()
                }
                missing = requested - existing
                if missing:
                    raise ValueError(f"Unknown site IDs: {sorted(missing)}")
            session.query(UserAllowedSiteModel).filter_by(user_id=user_id).delete(
                synchronize_session=False
            )
            for sid in sorted(requested):
                session.add(UserAllowedSiteModel(user_id=user_id, site_id=sid))

        row.updated_at = datetime.now(timezone.utc)
        session.flush()
        session.refresh(row)
        result = _to_user_read(row)

    logger.info("User %d updated", user_id)
    return result


def deactivate_user(user_id: int) -> UserRead:
    """Soft-delete a user. Raises ValueError if this is the last active admin."""
    return update_user(user_id, UserUpdate(is_active=False))


def verify_password(username: str, plain_password: str) -> bool:
    """Return True if plain_password matches the stored bcrypt hash for the user."""
    with get_session() as session:
        row = _get_row_by_username(username, session)
        if row is None or not row.is_active:
            return False
        return _pwd_context.verify(plain_password, row.hashed_password)


def update_password(user_id: int, new_password: str) -> None:
    """Re-hash and store a new password for the given user."""
    if len(new_password) < 8:
        raise ValueError("Password must be at least 8 characters")
    with get_session() as session:
        row = _get_row_by_id(user_id, session)
        if row is None:
            raise ValueError(f"User {user_id} not found")
        row.hashed_password = _pwd_context.hash(new_password)
        row.updated_at = datetime.now(timezone.utc)
    logger.info("Password updated for user %d", user_id)


def authenticate(username: str, password: str) -> Optional[UserRead]:
    """Verify credentials and return the user's public data, or None on failure."""
    with get_session() as session:
        row = _get_row_by_username(username, session)
        if row is None or not row.is_active:
            return None
        if not _pwd_context.verify(password, row.hashed_password):
            return None
        return _to_user_read(row)
