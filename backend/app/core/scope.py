"""MSP: Phase 3 — per-scope authorization dependencies.

Every scoped endpoint uses ``Depends(require_scope("<operation>"))``. To learn
what any endpoint requires, ``grep "require_scope" backend/app/api/`` and
cross-reference the matches against :data:`OP_MIN_ROLE`.

The table below is the *single source of truth* for the operation → min-role
mapping. If a new op is added to the API layer it must be added here (or the
dependency raises 500 at request time). This is the compact-and-traceable
authorization matrix promised by phase-3 §6.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

from fastapi import Depends, HTTPException, Request
from jwt import PyJWTError

from app.core.security import verify_token
from app.db.models import DeviceGroupModel, DeviceModel, SiteModel, UserModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope
from app.repositories.role_assignment_repository import RoleAssignmentRepository

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Operation → (scope_kind, min_role) matrix.
#
# scope_kind selects the resource-resolution rule. min_role uses the
# _ROLE_LEVEL ordering below.
#
# Ops that need custom dispatch (e.g. move_device same-site vs cross-site) are
# NOT in this table — they take a dedicated branch inside require_scope().
# ─────────────────────────────────────────────────────────────────────────────
OP_MIN_ROLE: dict[str, Tuple[str, str]] = {
    # ── Device reads / writes ─────────────────────────────────────────────
    "read_device":                    ("device",       "observer"),
    "write_device_config":            ("device",       "operator"),   # VLAN/port ops
    "edit_device":                    ("device",       "admin"),
    "delete_device":                  ("device",       "admin"),
    # register_device is scoped by the target site/group from the request body.
    "register_device":                ("site",         "admin"),
    # move_device is a synthetic op handled by _authorize_move_device — it
    # dispatches to move_device_same_site or move_device_cross_site at
    # runtime once the source and target sites are known.
    "move_device_same_site":          ("device",       "operator"),
    "move_device_cross_site":         ("site",         "admin"),      # both sides
    # ── Group ─────────────────────────────────────────────────────────────
    "read_group":                     ("device_group", "observer"),
    "list_group_devices":             ("device_group", "observer"),
    "create_group":                   ("site",         "admin"),
    "edit_group":                     ("device_group", "admin"),
    "delete_group":                   ("device_group", "admin"),
    # ── Site ──────────────────────────────────────────────────────────────
    "read_site":                      ("site",         "observer"),
    "list_site_groups":               ("site",         "observer"),
    # site create/delete + PUT /users/{id}/system-admin use require_system_admin
    # (they are not per-scope), so they intentionally have no entry here.
}


_ROLE_LEVEL: dict[str, int] = {
    "observer":    1,
    "operator":    2,
    "admin":       3,
    "super-admin": 99,   # bypass — set by effective_role for is_system_admin
}


# ─── Authenticated dependency ───────────────────────────────────────────────

def get_current_user(request: Request) -> dict:
    """Decode the bearer JWT into ``{username, is_system_admin}``. Raises 401
    otherwise. Falls back to the legacy ``role`` claim (``admin`` /
    ``super-admin`` → is_system_admin=True) when the explicit
    ``is_system_admin`` claim is absent — keeps synthetic-JWT test fixtures
    working alongside real logins."""
    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = verify_token(token)
    except PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    is_sys = payload.get("is_system_admin")
    if is_sys is None:
        is_sys = (payload.get("role") or "").lower() in {"admin", "super-admin"}
    return {"username": username, "is_system_admin": bool(is_sys)}


def require_authenticated(current: dict = Depends(get_current_user)) -> dict:
    """Enrich the JWT-derived caller with ``id`` and the DB-backed
    ``is_system_admin`` bit — the JWT claim is trusted only when the row is
    missing (test/JWT-only fixtures), otherwise the DB is authoritative."""
    username = (current.get("username") or "").lower()
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    with get_session() as session:
        row = (
            session.query(UserModel.id, UserModel.is_active, UserModel.is_system_admin)
            .filter(UserModel.username == username)
            .first()
        )
    if row is None:
        # Fixture or pre-migration token — trust the JWT's claim but leave
        # ``id`` unset so ``effective_role`` returns None on any scoped read.
        return {**current, "id": None}
    if not row[1]:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return {**current, "id": int(row[0]), "is_system_admin": bool(row[2])}


def require_system_admin(current: dict = Depends(require_authenticated)) -> dict:
    """Gate for globally sensitive ops (site create/delete, /system-admin, unlock)."""
    if not current.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="This operation requires system-admin privilege",
        )
    return current


# ─── Scope dependency ───────────────────────────────────────────────────────

def obtener_scope(
    current: dict = Depends(require_authenticated),
) -> VisibilityScope:
    """Resolve the caller's full VisibilityScope once per request.

    FastAPI caches dependency results within a single request, so every
    ``require_scope``-guarded endpoint shares the one scope built here —
    no matter how many per-resource checks the handler runs.

    The repository is fetched via the composition-layer factory so tests
    can inject a fake through ``app.dependency_overrides`` without
    reaching for module-level singletons.
    """
    from app.composition import get_role_assignment_repo

    repo: RoleAssignmentRepository = get_role_assignment_repo()
    return repo.scope_de(current)


def require_scope(op: str):
    """FastAPI dependency factory for per-scope authorization.

    Resource-resolution rules:
      * Path params first: ``{name}``/``{device_name}`` → device;
        ``{group_id}`` → device_group; ``{site_id}`` → site.
      * If none matches, the body is inspected for ``device_group_id`` then
        ``site_id`` (in that order).

    The synthetic op ``"move_device"`` takes a dedicated branch that reads
    the source device's site and the target group/site from the body, then
    picks ``move_device_same_site`` or ``move_device_cross_site`` and (for
    cross-site) checks the caller's role on *both* sides.
    """
    async def dep(
        request: Request,
        current: dict = Depends(require_authenticated),
        scope: VisibilityScope = Depends(obtener_scope),
    ) -> dict:
        if op == "move_device":
            await _authorize_move_device(request, current, scope)
            return current

        cfg = OP_MIN_ROLE.get(op)
        if cfg is None:
            # Programming error surfaced loudly rather than silently allowed.
            raise HTTPException(status_code=500, detail=f"Unknown scope operation: {op}")
        scope_kind, min_role = cfg

        target = await _resolve_target(request, scope_kind)
        if target is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Could not resolve {scope_kind} target for operation "
                    f"'{op}' from path or body"
                ),
            )

        resolved = resolver_site_group(target, scope_kind)
        role = scope.rol_para(*resolved) if resolved is not None else None
        _enforce(role, min_role, op, scope_kind, target)
        return current

    return dep


# ─── Move-device dispatcher ─────────────────────────────────────────────────

async def _authorize_move_device(
    request: Request, current: dict, scope: VisibilityScope,
) -> None:
    """Dispatch ``move_device`` to same-site vs cross-site and enforce.

    Called only for ``POST /devices/{name}/move``. Semantics:
      * ``{name}`` from the path identifies the source device.
      * Body ``{"device_group_id": <int>}`` selects the target group.
      * Body ``{"device_group_id": null}`` → move to the source site's
        Default group (per decision D8 clarification: "remove group" is a
        device-level action).
    """
    name = request.path_params.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="move_device requires {name} in path")

    body = await _peek_json_body(request)
    body_group_id = body.get("device_group_id") if isinstance(body, dict) else None

    with get_session() as session:
        source = _lookup_device_scope(session, name)
        if source is None:
            # Device not found or not placed in a valid group yet. Let the
            # handler surface 404; deny here so we don't leak existence.
            raise HTTPException(status_code=404, detail=f"Device '{name}' not found")
        src_site_id, src_group_id = source

        if body_group_id is None:
            # D8: null → current site's Default group. Guaranteed to exist
            # because sites.default_group_id is populated at site creation.
            default_group_id = _site_default_group_id(session, src_site_id)
            if default_group_id is None:
                raise HTTPException(
                    status_code=500,
                    detail=f"Site {src_site_id} has no Default group (unexpected)",
                )
            target_group_id = default_group_id
            target_site_id = src_site_id
        else:
            target_group_id = int(body_group_id)
            row = (
                session.query(DeviceGroupModel.site_id)
                .filter(DeviceGroupModel.id == target_group_id)
                .first()
            )
            if row is None or row[0] is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Target device_group {target_group_id} does not exist",
                )
            target_site_id = row[0]

    same_site = target_site_id == src_site_id

    if same_site:
        role = scope.rol_para(src_site_id, src_group_id)
        _enforce(
            role, "operator", "move_device_same_site", "device", name,
        )
    else:
        # Cross-site: caller must be admin on BOTH source group's site and
        # target site (D16 — take the min of the two effective roles).
        src_role = scope.rol_para(src_site_id, src_group_id)
        dst_role = scope.rol_para(target_site_id, None)
        for label, role in (("source", src_role), ("target", dst_role)):
            _enforce(
                role, "admin", "move_device_cross_site",
                "site", target_site_id if label == "target" else src_site_id,
            )


# ─── Resource-resolution helpers ────────────────────────────────────────────

async def _resolve_target(request: Request, scope_kind: str):
    """Return the identifier of the resource this request targets, or None.

    Kind      Path-param search order                Body-fallback keys
    ────────  ─────────────────────────────────────  ────────────────────
    device    name, device_name                      (no fallback)
    device_group  group_id                           device_group_id
    site      site_id                                site_id (then site_id
                                                     derived from device_group_id)
    """
    pp = request.path_params
    if scope_kind == "device":
        # Path first, then body: ports/vlans endpoints carry the target as
        # ``data.device`` in the request body (single-device ops). Batch
        # endpoints (VLAN over multiple devices) are handled inside the
        # service; the dep only pre-validates the first target and the
        # service iterates for the rest.
        from_path = pp.get("name") or pp.get("device_name")
        if from_path:
            return from_path
        body = await _peek_json_body(request)
        if isinstance(body, dict):
            if body.get("device"):
                return str(body["device"])
            devices = body.get("devices")
            if isinstance(devices, list) and devices:
                return str(devices[0])
        return None
    if scope_kind == "device_group":
        if "group_id" in pp:
            try:
                return int(pp["group_id"])
            except (TypeError, ValueError):
                return None
        body = await _peek_json_body(request)
        if isinstance(body, dict) and body.get("device_group_id") is not None:
            try:
                return int(body["device_group_id"])
            except (TypeError, ValueError):
                return None
        return None
    if scope_kind == "site":
        if "site_id" in pp:
            try:
                return int(pp["site_id"])
            except (TypeError, ValueError):
                return None
        body = await _peek_json_body(request)
        if isinstance(body, dict):
            if body.get("site_id") is not None:
                try:
                    return int(body["site_id"])
                except (TypeError, ValueError):
                    return None
            # As a last resort, resolve the target site through a body-provided
            # device_group_id. Used by register_device when only group is given.
            if body.get("device_group_id") is not None:
                with get_session() as session:
                    row = (
                        session.query(DeviceGroupModel.site_id)
                        .filter(DeviceGroupModel.id == int(body["device_group_id"]))
                        .first()
                    )
                    return int(row[0]) if row and row[0] is not None else None
        return None
    return None


async def _peek_json_body(request: Request) -> dict:
    """Read and cache the request body as JSON, safe to call before handler.

    Starlette caches the raw body inside ``request._body`` after the first
    ``.body()`` call, so downstream FastAPI body parsing still succeeds.
    Returns ``{}`` on empty or non-JSON payload — do not raise on parse
    errors here; the handler will surface the real 422.
    """
    if request.method in ("GET", "HEAD", "DELETE"):
        # Bodies are technically legal on DELETE, but our API never uses them
        # for authz targets — skip the read to avoid touching the stream.
        return {}
    body = await request.body()
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolver_site_group(
    target, scope_kind: str,
) -> Optional[Tuple[int, Optional[int]]]:
    """Resolve a target into ``(site_id, device_group_id | None)`` — the
    coordinates ``VisibilityScope.rol_para()`` needs to answer in memory.

    Absorbs the SQL side of the old ``effective_role`` path: site targets
    need no query, device_group targets need one JOIN-less lookup, and
    device targets defer to ``_lookup_device_scope`` (which stays put
    because ``_authorize_move_device`` also needs it directly for the
    move dispatcher).

    Returns ``None`` when the target does not exist — the caller turns
    that into a 403 via ``_enforce(role=None, ...)``.
    """
    if scope_kind == "site":
        try:
            return (int(target), None)
        except (TypeError, ValueError):
            return None
    if scope_kind == "device_group":
        try:
            group_id = int(target)
        except (TypeError, ValueError):
            return None
        with get_session() as session:
            row = (
                session.query(DeviceGroupModel.site_id)
                .filter(DeviceGroupModel.id == group_id)
                .first()
            )
        if row is None or row[0] is None:
            return None
        return (int(row[0]), group_id)
    if scope_kind == "device":
        with get_session() as session:
            return _lookup_device_scope(session, str(target))
    raise ValueError(f"unknown scope_kind: {scope_kind!r}")


def _lookup_device_scope(session, name: str) -> Optional[Tuple[int, Optional[int]]]:
    """Return ``(site_id, device_group_id)`` for a device or None if missing.

    ``devices.device_group_id`` is NOT NULL and ``device_groups.site_id`` is
    NOT NULL post-Phase-4, so the join is total for every device that exists.
    """
    row = (
        session.query(
            DeviceModel.device_group_id,
            DeviceGroupModel.site_id.label("group_site_id"),
        )
        .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
        .filter(DeviceModel.name == name)
        .first()
    )
    if row is None:
        return None
    group_id, group_site_id = row
    return (int(group_site_id), int(group_id))


def _site_default_group_id(session, site_id: int) -> Optional[int]:
    row = (
        session.query(SiteModel.default_group_id)
        .filter(SiteModel.id == site_id)
        .first()
    )
    return int(row[0]) if row and row[0] is not None else None


def _enforce(role: Optional[str], min_role: str, op: str, scope_kind: str, target) -> None:
    if role is None:
        raise HTTPException(
            status_code=403,
            detail=(
                f"No role assignment for {scope_kind} {target}; "
                f"operation '{op}' requires >= {min_role}"
            ),
        )
    if _ROLE_LEVEL.get(role, 0) < _ROLE_LEVEL[min_role]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Role '{role}' on {scope_kind} {target} is below minimum "
                f"'{min_role}' for operation '{op}'"
            ),
        )
