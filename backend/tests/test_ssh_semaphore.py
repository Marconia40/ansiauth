"""Tests for the global SSH-slot semaphore in RedisCoordinator.

Covers Decision 5 of docs/SSH_REFRESH_PLAN.md: a system-wide cap on
concurrent ansible-runner executions to protect server resources
(RAM/FDs/subprocesses) against bursts. This is orthogonal to the
per-device ``bloquear()`` lock -- both coexist.

Tests focus on the in-process fallback (BoundedSemaphore) so they run
without a live Redis. The Redis Lua-script path is exercised
end-to-end via ``adquirir_slot`` when a REDIS_URL is available.
"""
import os
import threading
import time

import pytest

from app.services import redis_coordinator as rc_module
from app.services.redis_coordinator import RedisCoordinator


def _fresh_coordinator_no_redis() -> RedisCoordinator:
    """A coordinator with no REDIS_URL -- forces the in-process fallback."""
    return RedisCoordinator(redis_url="")


def test_ssh_semaphore_permits_up_to_max_concurrent():
    """N concurrent acquirers all succeed within the cap."""
    coord = _fresh_coordinator_no_redis()
    n = rc_module.MAX_CONCURRENT_SSH

    inside = 0
    max_inside = 0
    lock = threading.Lock()
    barrier = threading.Barrier(n)
    release = threading.Event()

    def worker():
        nonlocal inside, max_inside
        with coord.adquirir_slot():
            barrier.wait(timeout=5)
            with lock:
                inside += 1
                if inside > max_inside:
                    max_inside = inside
            release.wait(timeout=5)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    # All N should reach the barrier -- if the semaphore is stricter than N
    # they'd deadlock on adquirir_slot() before barrier.wait().
    time.sleep(0.3)
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert max_inside == n, f"Expected all {n} workers concurrent, saw peak={max_inside}"


def test_ssh_semaphore_blocks_beyond_max():
    """The (N+1)-th acquirer must wait until a slot is released."""
    coord = _fresh_coordinator_no_redis()
    n = rc_module.MAX_CONCURRENT_SSH

    holders_started = threading.Barrier(n + 1)  # N holders + main thread
    release_holders = threading.Event()

    def holder():
        with coord.adquirir_slot():
            holders_started.wait(timeout=5)
            release_holders.wait(timeout=10)

    holder_threads = [threading.Thread(target=holder) for _ in range(n)]
    for t in holder_threads:
        t.start()
    holders_started.wait(timeout=5)  # all N have their slot

    # Now try to acquire an (N+1)-th slot -- must block.
    extra_acquired = threading.Event()

    def extra():
        with coord.adquirir_slot():
            extra_acquired.set()

    extra_thread = threading.Thread(target=extra)
    extra_thread.start()

    # Give it a moment; must not acquire while all N slots are held.
    assert not extra_acquired.wait(timeout=0.4), (
        "Semaphore let a caller through when all slots were held"
    )

    # Release the holders -- the extra thread should now proceed.
    release_holders.set()
    for t in holder_threads:
        t.join(timeout=5)
    assert extra_acquired.wait(timeout=5), (
        "Extra caller never got a slot after holders released"
    )
    extra_thread.join(timeout=5)


def test_ssh_semaphore_releases_on_exception():
    """A slot must be released even if the ``with`` body raises."""
    coord = _fresh_coordinator_no_redis()
    n = rc_module.MAX_CONCURRENT_SSH

    # Exhaust the pool by acquiring and immediately failing N times.
    for _ in range(n * 2):
        with pytest.raises(RuntimeError, match="boom"):
            with coord.adquirir_slot():
                raise RuntimeError("boom")

    # If releases hadn't happened, this final acquire would hang forever.
    # Use a thread + timeout so the test fails fast on regression.
    got = threading.Event()

    def probe():
        with coord.adquirir_slot():
            got.set()

    t = threading.Thread(target=probe)
    t.start()
    assert got.wait(timeout=2), "Semaphore was not released after exception in with-block"
    t.join(timeout=2)


@pytest.mark.skipif(
    not os.getenv("REDIS_URL"),
    reason="Redis-path test requires REDIS_URL to be set",
)
def test_ssh_semaphore_redis_path_permits_and_releases():
    """End-to-end sanity check against a live Redis: acquire, release, re-acquire."""
    coord = RedisCoordinator()
    # Clear any leftover state from previous runs.
    r = coord._get_redis()
    assert r is not None, "REDIS_URL set but coordinator couldn't reach Redis"
    r.delete(rc_module._SSH_SLOT_KEY)

    with coord.adquirir_slot() as slot_id:
        assert slot_id is not None
        # The slot must be counted as active in the sorted set.
        assert r.zcard(rc_module._SSH_SLOT_KEY) == 1

    # After the with-block, the slot must be gone.
    assert r.zcard(rc_module._SSH_SLOT_KEY) == 0

    # A second acquire must succeed -- releases are actually happening.
    with coord.adquirir_slot():
        assert r.zcard(rc_module._SSH_SLOT_KEY) == 1
    assert r.zcard(rc_module._SSH_SLOT_KEY) == 0


@pytest.mark.skipif(
    not os.getenv("REDIS_URL"),
    reason="Redis-path test requires REDIS_URL to be set",
)
def test_ssh_semaphore_redis_reclaims_expired_leases(monkeypatch):
    """A slot whose lease has expired is auto-reclaimed by the acquire script."""
    coord = RedisCoordinator()
    r = coord._get_redis()
    assert r is not None
    r.delete(rc_module._SSH_SLOT_KEY)

    # Simulate a crashed worker: seed the sorted set with fake slots whose
    # scores are older than the lease TTL.
    stale_ts = time.time() - (rc_module.SSH_SLOT_LEASE_S + 60)
    for i in range(rc_module.MAX_CONCURRENT_SSH):
        r.zadd(rc_module._SSH_SLOT_KEY, {f"stale-{i}": stale_ts})
    assert r.zcard(rc_module._SSH_SLOT_KEY) == rc_module.MAX_CONCURRENT_SSH

    # Even though the set looks full, the Lua script evicts expired entries
    # before counting -- this acquire must succeed without blocking on the poll.
    got = threading.Event()

    def probe():
        with coord.adquirir_slot():
            got.set()

    t = threading.Thread(target=probe)
    t.start()
    assert got.wait(timeout=3), "Acquire didn't reclaim expired leases"
    t.join(timeout=3)

    # Clean up.
    r.delete(rc_module._SSH_SLOT_KEY)
