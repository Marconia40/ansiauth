import logging
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def get_device_lock(device_id: str) -> threading.Lock:
    """Return (creating if needed) the per-device threading lock."""
    with _meta_lock:
        if device_id not in _locks:
            _locks[device_id] = threading.Lock()
        return _locks[device_id]


def is_device_busy(device_id: str) -> bool:
    """Return True if another thread currently holds this device's lock."""
    lock = get_device_lock(device_id)
    acquired = lock.acquire(blocking=False)
    if acquired:
        lock.release()
    return not acquired


@contextmanager
def acquire(device_id: str, timeout: float | None = None):
    """Serialise all SSH/Ansible access to *device_id* with lifecycle logging.

    Usage::

        with device_locks.acquire("huawei1"):
            run_playbook(...)

    Raises ``TimeoutError`` if the lock cannot be acquired within *timeout*
    seconds (pass ``None`` to block indefinitely).
    """
    lock = get_device_lock(device_id)
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
