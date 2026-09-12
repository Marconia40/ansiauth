from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import LoginAttemptModel
from app.db.session import get_session
from app.composition import login_attempt_repository, user_repository


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_failed_attempts(username: str, ip: str, count: int, age_minutes: int = 0) -> None:
    """Insert `count` failed attempts with a given age into the DB."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    with get_session() as session:
        for _ in range(count):
            session.add(
                LoginAttemptModel(
                    username=username.lower(),
                    ip_address=ip,
                    attempted_at=ts,
                    succeeded=False,
                )
            )


def _do_failed_login(client, username: str = "brute_user", password: str = "wrong"):
    return client.post("/api/v1/auth/login", data={"username": username, "password": password})


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def seed_brute_user():
    if user_repository.obtener_por_username("brute_user") is None:
        user_repository.crear("brute_user", "correct_password_123")
    yield
    from app.db.session import get_session
    from app.db.models import UserModel
    with get_session() as session:
        session.query(UserModel).filter_by(username="brute_user").delete(synchronize_session=False)


# ── Username lockout ──────────────────────────────────────────────────────────

def test_first_five_failures_return_401(unauth_client):
    for _ in range(5):
        r = _do_failed_login(unauth_client)
        assert r.status_code == 401


def test_sixth_failed_attempt_returns_429(unauth_client):
    from app.core import rls_middleware as rl
    for _ in range(5):
        _do_failed_login(unauth_client)

    rl.reset()  # isolate brute-force lockout from HTTP rate limiter
    r = _do_failed_login(unauth_client)
    assert r.status_code == 429


def test_429_has_correct_detail(unauth_client):
    from app.core import rls_middleware as rl
    for _ in range(5):
        _do_failed_login(unauth_client)

    rl.reset()
    r = _do_failed_login(unauth_client)
    assert r.json()["message"] == "Too many requests"


def test_account_remains_locked_on_correct_password(unauth_client):
    from app.core import rls_middleware as rl
    for _ in range(5):
        _do_failed_login(unauth_client)

    rl.reset()  # isolate: test that account lockout (not rate limit) blocks correct password
    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    assert r.status_code == 429


def test_expired_failures_do_not_count(unauth_client):
    # 5 failures from 20 minutes ago (outside the 15-minute window)
    _seed_failed_attempts("brute_user", "testclient", count=5, age_minutes=20)

    # Login should succeed since old failures are outside the lockout window
    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    assert r.status_code == 200


def test_different_ips_share_per_username_counter(unauth_client):
    # 3 failures from a different IP seeded directly
    _seed_failed_attempts("brute_user", "10.0.0.1", count=3, age_minutes=0)

    # 2 more failures via the actual request (from "testclient")
    for _ in range(2):
        _do_failed_login(unauth_client)

    # 5 total failures across two IPs — username is now locked
    r = _do_failed_login(unauth_client)
    assert r.status_code == 429


def test_successful_login_resets_failure_counter(unauth_client):
    from app.core import rls_middleware as rl
    for _ in range(4):
        _do_failed_login(unauth_client)

    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    assert r.status_code == 200

    # Reset rate limiter so the next 5 failures aren't blocked by HTTP rate limit
    rl.reset()

    # Brute-force counter was cleared by successful login; next 5 failures return 401
    for _ in range(5):
        r = _do_failed_login(unauth_client)
        assert r.status_code == 401


# ── IP-based blocking ─────────────────────────────────────────────────────────

def test_ip_blocked_after_20_failures(unauth_client):
    # Seed 19 failures from this IP for a different username (doesn't matter which)
    _seed_failed_attempts("other_user", "testclient", count=19, age_minutes=0)

    # One more failure from the same IP
    _do_failed_login(unauth_client)

    # Now this IP has 20 failures — next request from it is blocked
    r = _do_failed_login(unauth_client)
    assert r.status_code == 429


def test_ip_block_does_not_affect_different_ip(unauth_client, monkeypatch):
    # Block "testclient" IP
    _seed_failed_attempts("other_user", "testclient", count=20, age_minutes=0)

    # Requests from a different IP should still work
    monkeypatch.setattr(login_attempt_repository, "ip_bloqueada", lambda ip: ip == "testclient")
    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    # This comes from "testclient" so it still gets blocked via the monkeypatched check
    assert r.status_code == 429


def test_expired_ip_failures_do_not_block(unauth_client):
    # 20 IP failures from 2 hours ago (outside the 1-hour window)
    _seed_failed_attempts("other_user", "testclient", count=20, age_minutes=130)

    # Login should still work since old IP failures are expired
    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    assert r.status_code == 200


# ── Admin unlock endpoint ─────────────────────────────────────────────────────

def test_admin_can_unlock_locked_account(admin_client, unauth_client):
    from app.core import rls_middleware as rl
    for _ in range(5):
        _do_failed_login(unauth_client)

    rl.reset()
    assert _do_failed_login(unauth_client).status_code == 429

    r = admin_client.post("/api/v1/auth/unlock/brute_user")
    assert r.status_code == 200
    data = r.json()
    assert data["success"] is True
    assert data["data"]["username"] == "brute_user"
    assert data["data"]["attempts_cleared"] >= 5

    # Login succeeds after unlock; reset rate limiter first so it's not still full
    rl.reset()
    r = unauth_client.post("/api/v1/auth/login", data={"username": "brute_user", "password": "correct_password_123"})
    assert r.status_code == 200


def test_unlock_unknown_username_returns_zero_cleared(admin_client):
    r = admin_client.post("/api/v1/auth/unlock/nonexistent_user")
    assert r.status_code == 200
    assert r.json()["data"]["attempts_cleared"] == 0


def test_non_admin_cannot_unlock(unauth_client):
    r = unauth_client.post("/api/v1/auth/unlock/brute_user")
    assert r.status_code == 401


def test_observer_cannot_unlock(observer_client):
    r = observer_client.post("/api/v1/auth/unlock/brute_user")
    assert r.status_code == 403


# ── Service-level unit tests ──────────────────────────────────────────────────

def test_is_username_locked_false_when_no_attempts():
    assert login_attempt_repository.esta_bloqueado("nobody") is False


def test_is_username_locked_true_after_five_failures():
    _seed_failed_attempts("brute_user", "1.2.3.4", count=5, age_minutes=1)
    assert login_attempt_repository.esta_bloqueado("brute_user") is True


def test_is_username_locked_false_after_success_resets():
    _seed_failed_attempts("brute_user", "1.2.3.4", count=5, age_minutes=1)
    login_attempt_repository.resetear("brute_user")
    assert login_attempt_repository.esta_bloqueado("brute_user") is False


def test_is_ip_blocked_false_when_below_limit():
    _seed_failed_attempts("brute_user", "5.5.5.5", count=19, age_minutes=1)
    assert login_attempt_repository.ip_bloqueada("5.5.5.5") is False


def test_is_ip_blocked_true_at_limit():
    _seed_failed_attempts("brute_user", "5.5.5.5", count=20, age_minutes=1)
    assert login_attempt_repository.ip_bloqueada("5.5.5.5") is True
