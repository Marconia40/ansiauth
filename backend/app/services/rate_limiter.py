import logging
import threading
import time

logger = logging.getLogger(__name__)

_device_timestamps: dict[str, list[float]] = {}
_meta_lock = threading.Lock()

MAX_JOBS_PER_WINDOW: int = 5
WINDOW_SECONDS: float = 60.0


def wait_for_slot(device_id: str) -> None:
    """Block until a rate limit slot is available for this device, then claim it."""
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
    """Clear rate limit state. Used in tests."""
    with _meta_lock:
        if device_id:
            _device_timestamps.pop(device_id, None)
        else:
            _device_timestamps.clear()
