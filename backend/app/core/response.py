"""Shared success-envelope helper for api/*.py.

Every endpoint used to hand-build ``{"success": True, ...}`` (48 call sites
across the 9 files). ``ok()`` is the single place that shape is built.
"""
from typing import Any

_UNSET = object()


def ok(data: Any = _UNSET, **extra) -> dict:
    """Standard success envelope.

    ``data`` is omitted entirely when not passed -- several endpoint
    families (job listings, VLAN/port group-job responses) never had a
    ``"data"`` key at all; injecting ``"data": None`` there would be a
    wire-format regression.
    """
    body: dict = {"success": True}
    if data is not _UNSET:
        body["data"] = data
    body.update(extra)
    return body
