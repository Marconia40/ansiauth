"""Tests for sync-coalescing helpers on RedisCoordinator.

Coalescing avoids enqueuing duplicate ``sync_device_task``s while an
earlier one is still pending: two dashboard opens at the same time
should trigger one refresh, not two. Used by both the reactive endpoint
(``POST /dashboard/refresh``) and the scheduler (``sync_stale_devices_task``).

Coalescing is a best-effort optimization: when Redis is unreachable the
helpers degrade to "no coalescing" (each pending check returns False),
so the fallback is exercised too.
"""
import os

import pytest

from app.services.redis_coordinator import RedisCoordinator


def _fresh_coordinator_no_redis() -> RedisCoordinator:
    """Coordinator with Redis disabled -- exercises the fallback path."""
    return RedisCoordinator(redis_url="")


def test_coalescing_no_redis_never_reports_pending():
    """Fallback path: without Redis we never coalesce -- pending is always False."""
    coord = _fresh_coordinator_no_redis()
    assert coord.hay_sync_pendiente("dev-1", "all") is False
    coord.marcar_sync_pendiente("dev-1", "all")
    # Still False -- marker never persisted anywhere.
    assert coord.hay_sync_pendiente("dev-1", "all") is False
    coord.limpiar_sync_pendiente("dev-1", "all")  # no-op, must not raise


@pytest.mark.skipif(
    not os.getenv("REDIS_URL"),
    reason="Redis-path test requires REDIS_URL",
)
def test_coalescing_marks_then_detects_then_clears():
    """Live Redis: full lifecycle of a coalescing marker."""
    coord = RedisCoordinator()
    r = coord._get_redis()
    assert r is not None
    r.delete("sync_pending:dev-t1:all")

    assert coord.hay_sync_pendiente("dev-t1", "all") is False
    coord.marcar_sync_pendiente("dev-t1", "all", ttl_s=60)
    assert coord.hay_sync_pendiente("dev-t1", "all") is True

    # Different scope -- independent marker.
    assert coord.hay_sync_pendiente("dev-t1", "vlans") is False

    coord.limpiar_sync_pendiente("dev-t1", "all")
    assert coord.hay_sync_pendiente("dev-t1", "all") is False


@pytest.mark.skipif(
    not os.getenv("REDIS_URL"),
    reason="Redis-path test requires REDIS_URL",
)
def test_coalescing_marker_ttl_expires():
    """A marker with a very short TTL should auto-expire -- protects
    against markers that never get cleared (worker never runs)."""
    import time

    coord = RedisCoordinator()
    r = coord._get_redis()
    assert r is not None
    r.delete("sync_pending:dev-t2:all")

    coord.marcar_sync_pendiente("dev-t2", "all", ttl_s=1)
    assert coord.hay_sync_pendiente("dev-t2", "all") is True
    time.sleep(1.5)
    assert coord.hay_sync_pendiente("dev-t2", "all") is False


@pytest.mark.skipif(
    not os.getenv("REDIS_URL"),
    reason="Redis-path test requires REDIS_URL",
)
def test_coalescing_isolated_per_device():
    """Two devices' markers are independent."""
    coord = RedisCoordinator()
    r = coord._get_redis()
    assert r is not None
    r.delete("sync_pending:dev-a:all")
    r.delete("sync_pending:dev-b:all")

    coord.marcar_sync_pendiente("dev-a", "all")
    assert coord.hay_sync_pendiente("dev-a", "all") is True
    assert coord.hay_sync_pendiente("dev-b", "all") is False

    coord.limpiar_sync_pendiente("dev-a", "all")
