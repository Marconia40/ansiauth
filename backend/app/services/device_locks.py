import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# ── In-process fallback ───────────────────────────────────────────────────────
_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()

_redis_client = None
_redis_checked: bool = False


def _get_inprocess_lock(device_id: str) -> threading.Lock:
    with _meta_lock:
        if device_id not in _locks:
            _locks[device_id] = threading.Lock()
        return _locks[device_id]


def _get_redis():
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis as _redis
        r = _redis.from_url(url, socket_connect_timeout=2)
        r.ping()
        _redis_client = r
        logger.info("Device locks: using Redis at %s", url)
    except Exception as exc:
        logger.warning("Device locks: Redis unavailable (%s), using in-process fallback", exc)
        _redis_client = None
    return _redis_client


# Keep the same public API so orchestration runner and tests need no changes.

def get_device_lock(device_id: str) -> threading.Lock:
    """Return (creating if needed) the per-device threading lock."""
    return _get_inprocess_lock(device_id)


def is_device_busy(device_id: str) -> bool:
    """Return True if another worker currently holds this device's lock."""
    r = _get_redis()
    if r is not None:
        try:
            return r.exists(f"device_lock:{device_id}") > 0
        except Exception:
            pass
    lock = _get_inprocess_lock(device_id)
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
    return not acquired


@contextmanager
def acquire(device_id: str, timeout: float | None = None):
    """Serialise all SSH/Ansible access to *device_id*.

    Uses a Redis distributed lock when Redis is reachable; falls back to
    a threading.Lock for single-process deployments and tests.

    Raises ``TimeoutError`` if the lock cannot be acquired within *timeout*
    seconds (pass ``None`` to block indefinitely).
    """
    r = _get_redis()
    use_redis = False
    redis_lock = None

    if r is not None:
        try:
            redis_lock = r.lock(f"device_lock:{device_id}", timeout=300)
            logger.info("Waiting for device lock device=%s", device_id)
            # redis-py Lock.acquire: blocking_timeout=None → wait forever,
            # blocking_timeout=N → give up after N seconds.  Never pass -1;
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
        lock = _get_inprocess_lock(device_id)
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
