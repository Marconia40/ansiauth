import logging
import os
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

MAX_JOBS_PER_WINDOW: int = 5
WINDOW_SECONDS: float = 60.0
_REDIS_RECHECK_SECONDS: float = 30.0


class RedisCoordinator:
    """FINAL_ARCHITECTURE.md §1.6 — fuses device_locks.py + rate_limiter.py
    into a single coordinator per process. ``bloquear()``/``esta_ocupado()``
    serialise SSH/Ansible access to a device; ``limitar()``/``resetear()``
    enforce the per-device rate limit. Both paths delegate to Redis when
    reachable and fall back to in-process state when not.

    Fixes the permanent-fallback bug inherited from device_locks.py /
    rate_limiter.py (RNF-ESCAL-01): the original modules set
    ``_redis_checked = True`` on the first probe and never retried, so a
    single startup blip locked the process into ``threading.Lock``-only mode
    for its whole lifetime — defeating the "multiple backend instances,
    consistent locking" guarantee exactly when it mattered. This class
    reprobes every ``_REDIS_RECHECK_SECONDS`` while no client is cached, so
    a Redis that came up after the process did is picked up on the next
    operation.
    """

    def __init__(self, redis_url: str | None = None):
        self._redis_url = redis_url if redis_url is not None else os.getenv("REDIS_URL")
        self._redis_client = None
        self._last_check: float = 0.0
        self._locks: dict[str, threading.Lock] = {}
        self._device_timestamps: dict[str, list[float]] = {}
        self._meta_lock = threading.Lock()

    def _get_redis(self):
        if self._redis_client is not None:
            return self._redis_client
        now = time.time()
        if now - self._last_check < _REDIS_RECHECK_SECONDS:
            return None
        self._last_check = now
        if not self._redis_url:
            return None
        try:
            import redis as _redis
            r = _redis.from_url(self._redis_url, socket_connect_timeout=2)
            r.ping()
            self._redis_client = r
            logger.info("RedisCoordinator: using Redis at %s", self._redis_url)
            return r
        except Exception as exc:
            logger.warning("RedisCoordinator: Redis unavailable (%s), using in-process fallback", exc)
            return None

    def _get_inprocess_lock(self, device_id: str) -> threading.Lock:
        with self._meta_lock:
            if device_id not in self._locks:
                self._locks[device_id] = threading.Lock()
            return self._locks[device_id]

    # ── Device lock (device_locks.py) ─────────────────────────────────────

    @contextmanager
    def bloquear(self, device_id: str, timeout: float | None = None):
        """Serialise all SSH/Ansible access to *device_id*.

        Uses a Redis distributed lock when Redis is reachable; falls back to
        a threading.Lock for single-process deployments and tests.

        Raises ``TimeoutError`` if the lock cannot be acquired within
        *timeout* seconds (pass ``None`` to block indefinitely).
        """
        r = self._get_redis()
        use_redis = False
        redis_lock = None

        if r is not None:
            try:
                redis_lock = r.lock(f"device_lock:{device_id}", timeout=300)
                logger.info("Waiting for device lock device=%s", device_id)
                # redis-py Lock.acquire: blocking_timeout=None → wait forever,
                # blocking_timeout=N → give up after N seconds. Never pass -1;
                # redis-py treats negative values as "give up immediately".
                acquired = redis_lock.acquire(blocking=True, blocking_timeout=timeout)
                if not acquired:
                    limit = f"{timeout}s" if timeout is not None else "the timeout"
                    raise TimeoutError(
                        f"Could not acquire device lock for '{device_id}' within {limit} — device busy"
                    )
                use_redis = True
                logger.info("Device lock acquired device=%s", device_id)
            except TimeoutError:
                raise
            except Exception as exc:
                logger.warning("Device lock: Redis acquisition failed (%s), using in-process", exc)
                redis_lock = None

        if use_redis:
            try:
                yield
            finally:
                try:
                    redis_lock.release()
                except Exception:
                    pass
                logger.info("Device lock released device=%s", device_id)
        else:
            lock = self._get_inprocess_lock(device_id)
            logger.info("Waiting for device lock device=%s", device_id)
            if timeout is not None:
                acquired = lock.acquire(timeout=timeout)
            else:
                lock.acquire()
                acquired = True
            if not acquired:
                raise TimeoutError(
                    f"Could not acquire device lock for '{device_id}' within {timeout}s — device busy"
                )
            logger.info("Device lock acquired device=%s", device_id)
            try:
                yield
            finally:
                lock.release()
                logger.info("Device lock released device=%s", device_id)

    def esta_ocupado(self, device_id: str) -> bool:
        """Return True if another worker currently holds this device's lock."""
        r = self._get_redis()
        if r is not None:
            try:
                return r.exists(f"device_lock:{device_id}") > 0
            except Exception:
                pass
        lock = self._get_inprocess_lock(device_id)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return not acquired

    # ── Rate limit (rate_limiter.py) ──────────────────────────────────────

    def limitar(self, device_id: str) -> None:
        """Block until a rate-limit slot is available for this device, then claim it."""
        r = self._get_redis()
        if r is not None:
            try:
                self._wait_redis(r, device_id)
                return
            except Exception as exc:
                logger.warning("Rate limiter: Redis error (%s), falling back to in-process", exc)
        self._wait_inprocess(device_id)

    def _wait_redis(self, r, device_id: str) -> None:
        key = f"rate_limit:{device_id}"
        while True:
            # Create the key with TTL only if it doesn't exist yet (new window).
            r.set(key, 0, ex=int(WINDOW_SECONDS), nx=True)
            count = r.incr(key)
            if count <= MAX_JOBS_PER_WINDOW:
                return
            # Over limit — undo the increment, then wait for the window to expire.
            r.decr(key)
            ttl = r.ttl(key)
            wait_time = max(ttl, 1) + 0.05
            logger.info("Rate limit: device=%s waiting %.2fs for slot", device_id, wait_time)
            time.sleep(wait_time)

    def _wait_inprocess(self, device_id: str) -> None:
        while True:
            now = time.time()
            with self._meta_lock:
                if device_id not in self._device_timestamps:
                    self._device_timestamps[device_id] = []
                cutoff = now - WINDOW_SECONDS
                self._device_timestamps[device_id] = [t for t in self._device_timestamps[device_id] if t > cutoff]
                if len(self._device_timestamps[device_id]) < MAX_JOBS_PER_WINDOW:
                    self._device_timestamps[device_id].append(now)
                    return
                oldest = min(self._device_timestamps[device_id])
                wait_time = (oldest + WINDOW_SECONDS) - now + 0.05
            logger.info("Rate limit: device=%s waiting %.2fs for slot", device_id, wait_time)
            time.sleep(max(wait_time, 0.05))

    def resetear(self, device_id: str | None = None) -> None:
        """Clear rate-limit state. Used in tests and the conftest autouse fixture."""
        r = self._get_redis()
        if r is not None:
            try:
                if device_id:
                    r.delete(f"rate_limit:{device_id}")
                else:
                    for key in r.scan_iter("rate_limit:*"):
                        r.delete(key)
            except Exception:
                pass
        with self._meta_lock:
            if device_id:
                self._device_timestamps.pop(device_id, None)
            else:
                self._device_timestamps.clear()
