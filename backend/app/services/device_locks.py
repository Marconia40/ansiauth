import threading

_locks: dict[str, threading.Lock] = {}
_meta_lock = threading.Lock()


def get_device_lock(device_id: str) -> threading.Lock:
    """Return (creating if needed) the per-device threading lock."""
    with _meta_lock:
        if device_id not in _locks:
            _locks[device_id] = threading.Lock()
        return _locks[device_id]
