"""Unit tests for the session-hardening logic in refresh_token_service.

Covers the four ``ValueError`` branches (invalid / replay / expired / idle /
absolute) plus the metadata propagation guarantees the endpoint depends on:
``session_id`` and ``session_started_at`` must survive rotation so absolute
lifetime is anchored to the original login, and ``parent_id`` must point at
the immediately previous row so the chain is walkable for forensics.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import RefreshTokenModel
from app.db.session import get_session
from app.services import refresh_token_service
from app.services.refresh_token_service import _hash


def _row_by_hash(raw: str) -> RefreshTokenModel:
    with get_session() as session:
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        # Detach for read-only assertions after the session closes.
        session.expunge(row)
    return row


def _force_last_used(raw: str, when: datetime) -> None:
    with get_session() as session:
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        row.last_used_at = when


def _force_session_started(raw: str, when: datetime) -> None:
    with get_session() as session:
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        row.session_started_at = when


# ── create() ─────────────────────────────────────────────────────────────────

def test_create_populates_session_metadata():
    raw = refresh_token_service.create(
        "alice", ip_address="10.0.0.5", user_agent="Mozilla/5.0"
    )
    row = _row_by_hash(raw)
    assert row.session_id and len(row.session_id) >= 16
    assert row.parent_id is None
    assert row.last_used_at is not None
    assert row.session_started_at == row.created_at
    assert row.ip_address == "10.0.0.5"
    assert row.user_agent == "Mozilla/5.0"


def test_create_truncates_long_user_agent():
    long_ua = "x" * 2000
    raw = refresh_token_service.create("alice", user_agent=long_ua)
    row = _row_by_hash(raw)
    assert row.user_agent is not None and len(row.user_agent) == 512


# ── validate_and_rotate() — happy path ───────────────────────────────────────

def test_rotate_returns_new_token_and_session_id():
    raw = refresh_token_service.create("alice")
    original_session_id = _row_by_hash(raw).session_id

    new_raw, username, session_id = refresh_token_service.validate_and_rotate(raw)

    assert new_raw != raw
    assert username == "alice"
    assert session_id == original_session_id


def test_rotate_inherits_session_id_and_started_at():
    raw1 = refresh_token_service.create("alice")
    original = _row_by_hash(raw1)

    raw2, _, _ = refresh_token_service.validate_and_rotate(raw1)
    raw3, _, _ = refresh_token_service.validate_and_rotate(raw2)

    row2 = _row_by_hash(raw2)
    row3 = _row_by_hash(raw3)
    assert row2.session_id == original.session_id
    assert row3.session_id == original.session_id
    # session_started_at must anchor to the original login, not each rotation
    def _naive(dt):
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    assert _naive(row3.session_started_at) == _naive(original.session_started_at)


def test_rotate_links_parent_id_to_previous_row():
    raw1 = refresh_token_service.create("alice")
    original_id = _row_by_hash(raw1).id

    raw2, _, _ = refresh_token_service.validate_and_rotate(raw1)

    assert _row_by_hash(raw2).parent_id == original_id


def test_rotate_updates_last_used_at_on_old_row():
    raw = refresh_token_service.create("alice")
    _force_last_used(raw, datetime.now(timezone.utc) - timedelta(minutes=5))
    _, _, _ = refresh_token_service.validate_and_rotate(raw)

    def _naive(dt):
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    now_naive = _naive(datetime.now(timezone.utc))
    delta = abs((now_naive - _naive(_row_by_hash(raw).last_used_at)).total_seconds())
    assert delta < 5


# ── validate_and_rotate() — error branches ───────────────────────────────────

def test_invalid_token_raises_invalid():
    with pytest.raises(ValueError, match="Invalid refresh token"):
        refresh_token_service.validate_and_rotate("garbage")


def test_replay_revokes_all_user_sessions():
    """Replay = potential theft → revoke every active token for that user
    (all session chains), not just the current one."""
    # Two independent sessions for the same user (two logins).
    chain_a_r1 = refresh_token_service.create("alice")
    chain_b_r1 = refresh_token_service.create("alice")

    # Rotate chain A once.
    chain_a_r2, _, _ = refresh_token_service.validate_and_rotate(chain_a_r1)

    # Replay the revoked r1 of chain A → cascade revocation across both chains.
    with pytest.raises(ValueError, match="already used"):
        refresh_token_service.validate_and_rotate(chain_a_r1)

    # chain A's rotated token is now dead.
    with pytest.raises(ValueError):
        refresh_token_service.validate_and_rotate(chain_a_r2)
    # chain B's r1 is also dead, even though it never rotated.
    with pytest.raises(ValueError):
        refresh_token_service.validate_and_rotate(chain_b_r1)


def test_idle_timeout_revokes_only_current_chain():
    """Idle = user walked away → cut this chain, keep the user's other
    sessions alive (they might be a phone on another network)."""
    other_chain = refresh_token_service.create("alice")
    idle_chain = refresh_token_service.create("alice")

    # Force idle_chain's last_used_at outside the idle window.
    from app.core.config import REFRESH_TOKEN_IDLE_MINUTES
    _force_last_used(
        idle_chain,
        datetime.now(timezone.utc) - timedelta(minutes=REFRESH_TOKEN_IDLE_MINUTES + 1),
    )

    with pytest.raises(ValueError, match="idle_timeout"):
        refresh_token_service.validate_and_rotate(idle_chain)

    # Other chain must still work.
    new_other, _, _ = refresh_token_service.validate_and_rotate(other_chain)
    assert new_other != other_chain


def test_absolute_lifetime_revokes_whole_chain():
    """Absolute limit = force re-login regardless of activity → cut every
    row in this session_id, but leave other sessions alone."""
    other_chain = refresh_token_service.create("alice")
    old_chain_r1 = refresh_token_service.create("alice")
    old_chain_r2, _, _ = refresh_token_service.validate_and_rotate(old_chain_r1)

    from app.core.config import SESSION_ABSOLUTE_MAX_HOURS
    _force_session_started(
        old_chain_r2,
        datetime.now(timezone.utc) - timedelta(hours=SESSION_ABSOLUTE_MAX_HOURS + 1),
    )

    with pytest.raises(ValueError, match="session_absolute_limit"):
        refresh_token_service.validate_and_rotate(old_chain_r2)

    # Other chain is untouched.
    new_other, _, _ = refresh_token_service.validate_and_rotate(other_chain)
    assert new_other != other_chain


def test_expired_ttl_raises_expired():
    raw = refresh_token_service.create("alice")
    with get_session() as session:
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        row.expires_at = datetime.utcnow() - timedelta(seconds=1)

    with pytest.raises(ValueError, match="expired"):
        refresh_token_service.validate_and_rotate(raw)


# ── Active sessions listing ──────────────────────────────────────────────────

def test_list_active_sessions_groups_by_session_id():
    refresh_token_service.create("alice", ip_address="10.0.0.1", user_agent="A")
    raw2 = refresh_token_service.create("alice", ip_address="10.0.0.2", user_agent="B")
    # Rotate the second chain — the rotation row inherits the session_id
    # so both chains still collapse into two entries, not three.
    refresh_token_service.validate_and_rotate(
        raw2, ip_address="10.0.0.2", user_agent="B"
    )

    sessions = refresh_token_service.list_active_sessions("alice")
    assert len(sessions) == 2
    ips = sorted(s["ip_address"] for s in sessions)
    assert ips == ["10.0.0.1", "10.0.0.2"]
    # ``current`` unset → all False.
    assert all(s["current"] is False for s in sessions)


def test_list_active_sessions_flags_current():
    my_raw = refresh_token_service.create("alice")
    refresh_token_service.create("alice")

    my_sid = refresh_token_service.get_session_id_for_raw(my_raw)
    sessions = refresh_token_service.list_active_sessions(
        "alice", current_session_id=my_sid
    )
    current = [s for s in sessions if s["current"]]
    assert len(current) == 1
    assert current[0]["session_id"] == my_sid


def test_list_active_sessions_ignores_other_users():
    refresh_token_service.create("alice")
    refresh_token_service.create("bob")
    assert len(refresh_token_service.list_active_sessions("alice")) == 1


def test_list_active_sessions_hides_revoked_and_expired():
    raw_kept = refresh_token_service.create("alice")
    raw_expired = refresh_token_service.create("alice")
    raw_revoked = refresh_token_service.create("alice")

    with get_session() as session:
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw_expired)).one()
        row.expires_at = datetime.utcnow() - timedelta(hours=1)
        row2 = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw_revoked)).one()
        row2.revoked = True

    sessions = refresh_token_service.list_active_sessions("alice")
    kept_sid = refresh_token_service.get_session_id_for_raw(raw_kept)
    assert [s["session_id"] for s in sessions] == [kept_sid]


def test_revoke_other_sessions_leaves_only_current():
    my_raw = refresh_token_service.create("alice")
    other_raw_1 = refresh_token_service.create("alice")
    other_raw_2 = refresh_token_service.create("alice")

    my_sid = refresh_token_service.get_session_id_for_raw(my_raw)
    revoked = refresh_token_service.revoke_other_sessions("alice", my_sid)
    assert revoked == 2

    # Current session still rotates.
    new_my_raw, _, _ = refresh_token_service.validate_and_rotate(my_raw)
    assert new_my_raw != my_raw

    # Other sessions cannot rotate anymore.
    for dead in (other_raw_1, other_raw_2):
        with pytest.raises(ValueError):
            refresh_token_service.validate_and_rotate(dead)


def test_revoke_other_sessions_ignores_other_users():
    my_raw = refresh_token_service.create("alice")
    bob_raw = refresh_token_service.create("bob")

    my_sid = refresh_token_service.get_session_id_for_raw(my_raw)
    refresh_token_service.revoke_other_sessions("alice", my_sid)

    # Bob's session is untouched.
    new_bob, _, _ = refresh_token_service.validate_and_rotate(bob_raw)
    assert new_bob != bob_raw
