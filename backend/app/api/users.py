import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import (
    require_authenticated,
    require_elevated,
    require_system_admin,
)
from app.db.models import RoleAssignmentModel, UserModel
from app.db.session import get_session
from app.schemas.role_assignment import (
    RoleAssignmentCreate,
    SystemAdminUpdate,
)
from app.models.domain_event import DomainEvent
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services.role_assignment_service import RoleAssignmentService

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_read(user: User) -> dict:
    return UserRead(
        id=user.id, username=user.username, email=user.email,
        is_active=user.is_active, is_system_admin=user.is_system_admin,
        created_at=user.created_at, updated_at=user.updated_at,
    ).model_dump()


# ─── Scope helpers ──────────────────────────────────────────────────────────


def _actor_admin_site_ids(actor: dict) -> "set[int] | None":
    """Sites where the caller can act as user manager.

    Returns:
        * ``None`` when the caller is system-admin (no restriction).
        * A set of site ids where the caller holds a site-wide admin grant
          (``device_group_id IS NULL``). Group-scoped admins are excluded on
          purpose — user management is a site-level privilege (mirrors the
          rule in ``RoleAssignmentService._authorize_grant_or_revoke``).
    """
    if actor.get("is_system_admin"):
        return None
    from app.composition import role_assignment_repository
    scope = role_assignment_repository.scope_de(actor)
    return {sid for sid, gid, role in scope.grants if gid is None and role == "admin"}


def _target_user_site_ids(user_id: int) -> "set[int]":
    """Set of site ids where ``user_id`` has any grant (site-wide or group)."""
    with get_session() as session:
        rows = (
            session.query(RoleAssignmentModel.site_id)
            .filter(RoleAssignmentModel.user_id == user_id)
            .all()
        )
        return {int(r[0]) for r in rows}


def _require_can_manage_target(actor: dict, target_user_id: int) -> None:
    """Raise 403 unless ``actor`` may manage ``target_user_id``.

    Rules:
      * system-admin bypass.
      * self-management (updating own profile) is always allowed.
      * otherwise the target must hold at least one grant on a site where
        the actor is site-wide admin. A system-admin target is only
        manageable by another system-admin.
    """
    if actor.get("is_system_admin"):
        return
    if actor.get("id") == target_user_id:
        return
    admin_sites = _actor_admin_site_ids(actor) or set()
    if not admin_sites:
        raise HTTPException(
            status_code=403,
            detail="User management requires a site-wide admin grant or system-admin",
        )
    with get_session() as session:
        target = session.query(UserModel).filter_by(id=target_user_id).first()
    if target is None:
        raise NotFoundError(f"User {target_user_id} not found")
    if target.is_system_admin:
        raise HTTPException(
            status_code=403,
            detail="Only a system-admin can manage a system-admin user",
        )
    overlap = _target_user_site_ids(target_user_id) & admin_sites
    if not overlap:
        raise HTTPException(
            status_code=403,
            detail=(
                "You may only manage users that hold a grant on a site where "
                "you are site-admin"
            ),
        )


@router.post(
    "/",
    summary="Create user",
    description=(
        "Create a new user account. System-admins may create with any "
        "``is_system_admin`` value and grants are optional. Site-admins may "
        "only create non-system-admin users and must supply "
        "``initial_grant`` on a site where they hold a site-wide admin "
        "grant — the new user is created together with that grant so the "
        "site-admin sees the row in the scoped list immediately. Passwords "
        "are hashed with PBKDF2-SHA256. If the username belongs to a "
        "soft-deleted account, it is reactivated in place (grants purged, "
        "credentials/is_system_admin reset from this call) — the audit "
        "trail keeps the original user id but a ``reactivate_user`` event "
        "is emitted instead of ``create_user``."
    ),
)
def create_user(data: UserCreate, current_user: dict = Depends(require_authenticated)):
    from app.composition import event_dispatcher, user_repository

    actor_is_system_admin = bool(current_user.get("is_system_admin"))
    if not actor_is_system_admin:
        if data.is_system_admin:
            raise HTTPException(
                status_code=403,
                detail="Only a system-admin can create system-admin users",
            )
        if data.initial_grant is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Site-admin callers must supply an initial_grant so the "
                    "new user is visible in the scoped users list"
                ),
            )
        # Pre-authorize the grant target before touching the users table so
        # a 403 does not leave an orphan user behind. Reuses the same rule
        # the grant() service applies.
        RoleAssignmentService()._authorize_grant_or_revoke(
            actor=current_user,
            site_id=data.initial_grant.site_id,
            device_group_id=data.initial_grant.device_group_id,
            role_being_granted=data.initial_grant.role,
        )

    try:
        user, reactivated = user_repository.crear_o_reactivar(
            username=data.username, password=data.password,
            email=data.email, is_system_admin=data.is_system_admin,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    event_dispatcher.despachar([DomainEvent(
        "reactivate_user" if reactivated else "create_user",
        user, None, current_user["username"],
        {"resource_id": user.id, "username": user.username, "reactivated": reactivated},
    )])

    if data.initial_grant is not None:
        RoleAssignmentService().grant(
            target_user_id=user.id,
            site_id=data.initial_grant.site_id,
            device_group_id=data.initial_grant.device_group_id,
            role=data.initial_grant.role,
            actor=current_user,
        )

    body = _to_read(user)
    body["reactivated"] = reactivated
    return ok(body)


@router.get(
    "/",
    summary="List users",
    description=(
        "Return user accounts visible to the caller. System-admins see "
        "every user. Site-admins see only users that hold at least one "
        "grant on a site where they are site-wide admin. Active users "
        "only by default; pass ``include_inactive=true`` to include "
        "deactivated accounts. Paginated — defaults to 50 per page."
    ),
)
def list_users(
    current_user: dict = Depends(require_authenticated),
    include_inactive: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    from app.composition import user_repository

    admin_sites = _actor_admin_site_ids(current_user)
    if admin_sites is not None and not admin_sites:
        raise HTTPException(
            status_code=403,
            detail="User management requires a site-wide admin grant or system-admin",
        )

    all_users = user_repository.listar(incluir_inactivos=include_inactive)

    if admin_sites is not None:
        with get_session() as session:
            rows = (
                session.query(
                    RoleAssignmentModel.user_id,
                    RoleAssignmentModel.site_id,
                )
                .filter(RoleAssignmentModel.site_id.in_(admin_sites))
                .all()
            )
        visible_user_ids = {int(r[0]) for r in rows}
        # Always include the caller's own row so a site-admin can see and
        # edit their own profile even before any grant lands on one of
        # their sites (the caller may only have group-scoped grants
        # themselves — irrelevant here, but the row must still show up).
        actor_id = current_user.get("id")
        if actor_id is not None:
            visible_user_ids.add(actor_id)
        all_users = [u for u in all_users if u.id in visible_user_ids]

    total = len(all_users)
    start = (page - 1) * page_size
    return ok({
        "items": [_to_read(u) for u in all_users[start : start + page_size]],
        "total": total, "page": page, "page_size": page_size,
    })


@router.get(
    "/{user_id}",
    summary="Get user",
    description=(
        "Return a single user by numeric ID. Allowed for system-admin, "
        "self, or a site-admin whose site the target holds a grant on."
    ),
)
def get_user(user_id: int, current_user: dict = Depends(require_authenticated)):
    if current_user.get("is_system_admin") or current_user.get("id") == user_id:
        pass  # allowed
    else:
        _require_can_manage_target(current_user, user_id)
    from app.composition import user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return ok(_to_read(user))


@router.put(
    "/{user_id}",
    summary="Update user",
    description=(
        "Update user fields: email, password, or active status. "
        "All fields are optional. The last active system-admin cannot be "
        "deactivated via this endpoint. Allowed for system-admin, self, "
        "or a site-admin managing a user in their site."
    ),
)
def update_user(
    user_id: int,
    data: UserUpdate,
    current_user: dict = Depends(require_authenticated),
):
    _require_can_manage_target(current_user, user_id)

    from app.composition import event_dispatcher, user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    try:
        if data.email is not None:
            email_norm = data.email.lower()
            clash = [u for u in user_repository.listar(incluir_inactivos=True) if u.email == email_norm]
            if clash and clash[0].id != user_id:
                raise ValidationError(f"Email '{email_norm}' is already registered")
            user.actualizar(email=data.email)
        if data.password is not None:
            user.actualizar(password=data.password)
        if data.is_active is False:
            if user_repository.es_ultimo_admin_activo(user_id):
                raise ValidationError("Cannot deactivate the last active system-admin account")
            user.desactivar()
        elif data.is_active is True:
            user.activar()
    except ValueError as e:
        raise ValidationError(str(e))
    user = user_repository.add(user)
    audit_fields = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "password"}
    if data.password is not None:
        audit_fields["password_changed"] = True
    event_dispatcher.despachar([DomainEvent(
        "update_user", user, None, current_user["username"],
        {"resource_id": user_id, "updated_fields": audit_fields},
    )])
    return ok(_to_read(user))


@router.delete(
    "/{user_id}",
    summary="Deactivate user",
    description=(
        "Soft-delete a user account. The last active system-admin cannot be "
        "deactivated. The user record is retained for audit purposes. "
        "Allowed for system-admin or a site-admin managing a user in their "
        "site."
    ),
)
def deactivate_user(
    user_id: int,
    current_user: dict = Depends(require_authenticated),
    _elevated: dict = Depends(require_elevated),
):
    _require_can_manage_target(current_user, user_id)

    from app.composition import event_dispatcher, user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    if user_repository.es_ultimo_admin_activo(user_id):
        raise ValidationError("Cannot deactivate the last active system-admin account")
    user.desactivar()
    user = user_repository.add(user)
    event_dispatcher.despachar([DomainEvent(
        "deactivate_user", user, None, current_user["username"],
        {"resource_id": user_id, "username": user.username},
    )])
    return ok({"id": user_id, "is_active": False})


# ─── MSP: grants / system-admin ─────────────────────────────────────────────


@router.post(
    "/{user_id}/grants",
    summary="Grant role assignment",
    description=(
        "Grant a role at (site) or (site, group) scope. Only system-admins or "
        "site-admins may issue grants (group-admins may not delegate — D25)."
    ),
    status_code=201,
)
def create_grant(
    user_id: int,
    body: RoleAssignmentCreate,
    current_user: dict = Depends(require_authenticated),
):
    try:
        record = RoleAssignmentService().grant(
            target_user_id=user_id,
            site_id=body.site_id,
            device_group_id=body.device_group_id,
            role=body.role,
            actor=current_user,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    return ok(record.model_dump())


@router.get(
    "/{user_id}/grants",
    summary="List role assignments for user",
    description=(
        "Return the target user's grants filtered by what the viewer may see: "
        "system-admins and the target themselves see everything; other admins "
        "see only grants at sites where they hold an admin grant."
    ),
)
def list_grants(user_id: int, current_user: dict = Depends(require_authenticated)):
    records = RoleAssignmentService().list_for_user(user_id, viewer=current_user)
    return ok([r.model_dump() for r in records])


@router.delete(
    "/{user_id}/grants/{grant_id}",
    summary="Revoke role assignment",
    description="Delete a specific grant. Same authz as POST.",
    status_code=204,
)
def delete_grant(
    user_id: int,
    grant_id: int,
    current_user: dict = Depends(require_authenticated),
    _elevated: dict = Depends(require_elevated),
):
    RoleAssignmentService().revoke(grant_id, actor=current_user)
    return Response(status_code=204)


@router.put(
    "/{user_id}/system-admin",
    summary="Toggle system-admin flag",
    description=(
        "Promote or demote a user to/from system-admin. The last active "
        "system-admin cannot be demoted. Requires system-admin."
    ),
)
def set_system_admin(
    user_id: int,
    body: SystemAdminUpdate,
    current_user: dict = Depends(require_system_admin),
    _elevated: dict = Depends(require_elevated),
):
    RoleAssignmentService().set_system_admin(
        target_user_id=user_id,
        is_system_admin=body.is_system_admin,
        actor=current_user,
    )
    return ok({"id": user_id, "is_system_admin": body.is_system_admin})
