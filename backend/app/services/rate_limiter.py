import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_device_timestamps: dict[str, list[float]] = {}
_meta_lock = threading.Lock()

MAX_JOBS_PER_WINDOW: int = 5
WINDOW_SECONDS: float = 60.0

_redis_client = None
_redis_checked: bool = False


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
        logger.info("Rate limiter: using Redis at %s", url)
    except Exception as exc:
        logger.warning("Rate limiter: Redis unavailable (%s), using in-process fallback", exc)
        _redis_client = None
    return _redis_client


def wait_for_slot(device_id: str) -> None:
    """Block until a rate-limit slot is available for this device, then claim it."""
    r = _get_redis()
    if r is not None:
        try:
            _wait_redis(r, device_id)
            return
        except Exception as exc:
            logger.warning("Rate limiter: Redis error (%s), falling back to in-process", exc)
    _wait_inprocess(device_id)


def _wait_redis(r, device_id: str) -> None:
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


def _wait_inprocess(device_id: str) -> None:
    while True:
        now = time.time()
        with _meta_lock:
            if device_id not in _device_timestamps:
                _device_timestamps[device_id] = []
            cutoff = now - WINDOW_SECONDS
            _device_timestamps[device_id] = [t for t in _device_timestamps[device_id] if t > cutoff]
            if len(_device_timestamps[device_id]) < MAX_JOBS_PER_WINDOW:
                _device_timestamps[device_id].append(now)
                return
            oldest = min(_device_timestamps[device_id])
            wait_time = (oldest + WINDOW_SECONDS) - now + 0.05
        logger.info("Rate limit: device=%s waiting %.2fs for slot", device_id, wait_time)
        time.sleep(max(wait_time, 0.05))


def reset(device_id: str | None = None) -> None:
    """Clear rate-limit state. Used in tests and the conftest autouse fixture."""
    r = _get_redis()
    if r is not None:
        try:
            if device_id:
                r.delete(f"rate_limit:{device_id}")
            else:
                for key in r.scan_iter("rate_limit:*"):
                    r.delete(key)
        except Exception:
            pass
    with _meta_lock:
        if device_id:
            _device_timestamps.pop(device_id, None)
        else:
            _device_timestamps.clear()
