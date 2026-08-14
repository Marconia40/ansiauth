from datetime import datetime, timedelta, timezone

from app.db.models import LoginAttemptModel
from app.db.session import get_session

USERNAME_FAIL_LIMIT: int = 5
USERNAME_LOCKOUT_MINUTES: int = 15
IP_FAIL_LIMIT: int = 20
IP_LOCKOUT_HOURS: int = 1


def is_username_locked(username: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=USERNAME_LOCKOUT_MINUTES)
    with get_session() as session:
        count = (
            session.query(LoginAttemptModel)
            .filter(
                LoginAttemptModel.username == username.lower(),
                LoginAttemptModel.succeeded == False,  # noqa: E712
                LoginAttemptModel.attempted_at >= cutoff,
            )
            .count()
        )
    return count >= USERNAME_FAIL_LIMIT


def is_ip_blocked(ip_address: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=IP_LOCKOUT_HOURS)
    with get_session() as session:
        count = (
            session.query(LoginAttemptModel)
            .filter(
                LoginAttemptModel.ip_address == ip_address,
                LoginAttemptModel.succeeded == False,  # noqa: E712
                LoginAttemptModel.attempted_at >= cutoff,
            )
            .count()
        )
    return count >= IP_FAIL_LIMIT


def record_attempt(username: str, ip_address: str, succeeded: bool) -> None:
    with get_session() as session:
        session.add(
            LoginAttemptModel(
                username=username.lower(),
                ip_address=ip_address,
                attempted_at=datetime.now(timezone.utc),
                succeeded=succeeded,
            )
        )


def reset_username_failures(username: str) -> None:
    with get_session() as session:
        session.query(LoginAttemptModel).filter(
            LoginAttemptModel.username == username.lower(),
            LoginAttemptModel.succeeded == False,  # noqa: E712
        ).delete(synchronize_session=False)


def unlock_username(username: str) -> int:
    with get_session() as session:
        count = session.query(LoginAttemptModel).filter(
            LoginAttemptModel.username == username.lower(),
            LoginAttemptModel.succeeded == False,  # noqa: E712
        ).delete(synchronize_session=False)
    return count


def reset_all() -> None:
    """Clear all login attempt records. Used in tests."""
    with get_session() as session:
        session.query(LoginAttemptModel).delete(synchronize_session=False)
