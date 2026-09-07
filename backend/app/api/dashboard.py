"""GET /api/v1/dashboard/summary — agregación server-side para el
frontend Dashboard.

Reemplaza el patrón N+1: antes, para un site con N devices, el
frontend disparaba 1 + 3N requests (getDevices + getVlansSynced *N +
getPortsSynced *N + getJobs). Ahora dispara 1 sola.

Toda la lógica de conteo vive en DashboardService. Este archivo es
un thin wrapper: valida params, delega al service, envuelve en ok().
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import obtener_scope, require_authenticated
from app.models.visibility_scope import VisibilityScope

router = APIRouter()


@router.get(
    "/summary",
    summary="Dashboard aggregation for a scope",
    description=(
        "Return aggregated counters (devices/vlans/ports/svis/jobs) used "
        "by the dashboard cards. Replaces the N+1 pattern where the "
        "frontend fetched the device list plus one vlans/ports/svis query "
        "per device.\n\n"
        "Scopes soportados via ?scope=:\n"
        "* `org`   — todos los devices visibles al caller\n"
        "* `site`  — devices del site (requiere ?id=<site_id>)\n"
        "* `group` — devices del device_group (requiere ?id=<group_id>)\n"
        "* `device`— un solo device (requiere ?name=<device_name>)\n\n"
        "Autorización: cualquier user autenticado. Si el user no tiene "
        "visibilidad sobre nada del scope pedido, la respuesta vuelve "
        "con contadores en cero (no 403). Si el site/group/device de la "
        "URL no existe: 404."
    ),
)
def get_dashboard_summary(
    scope: str = Query(
        ...,
        pattern="^(org|site|group|device)$",
        description="Tipo de agregación",
    ),
    id: Optional[int] = Query(
        default=None,
        ge=1,
        description="Site o group ID. Requerido para scope=site y scope=group.",
    ),
    name: Optional[str] = Query(
        default=None,
        description="Device name. Requerido para scope=device.",
    ),
    jobs_days: int = Query(
        default=7,
        ge=1,
        le=30,
        description="Ventana temporal de jobs a agregar (1–30 días).",
    ),
    current_user: dict = Depends(require_authenticated),
    visibility_scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import dashboard_service

    # Validación de combinaciones de params.
    if scope in ("site", "group") and id is None:
        raise ValidationError(f"scope={scope} requires 'id' query param")
    if scope == "device" and not name:
        raise ValidationError("scope=device requires 'name' query param")
    if scope == "org" and (id is not None or name is not None):
        # No es crítico pero avisamos: pasar id/name en org es sin efecto y
        # probablemente indica bug en el caller.
        raise ValidationError(
            "scope=org does not accept 'id' or 'name' params",
        )

    payload = dashboard_service.resumir(
        scope_kind=scope,
        scope_id=id,
        device_name=name,
        jobs_days=jobs_days,
        visibility_scope=visibility_scope,
    )

    if payload is None:
        # El scope target (site/group/device) no existe en la DB. El
        # service devuelve None en ese caso; acá lo convertimos al 404.
        target = f"{scope}={id or name}"
        raise NotFoundError(f"Scope target '{target}' not found")

    return ok(payload)


# Los tres scopes que sync_device_task acepta (ver backend/app/tasks.py).
# Refresh siempre encola los tres — sincronizar solo uno rara vez tiene
# sentido desde el dashboard y complica el contrato sin beneficio real.
_SYNC_SCOPES = ("vlans", "ports", "svis")


@router.post(
    "/refresh",
    status_code=202,
    summary="Refresh sync cache for every device in a scope",
    description=(
        "Encola en Celery una sync de VLANs + ports + SVIs para cada "
        "device visible dentro del scope. Reemplaza el patrón anterior "
        "donde el frontend iteraba por device disparando 3 requests por "
        "cada uno (3N requests). Ahora es 1 sola request → M tareas en "
        "background (con M = 3 × devices visibles).\n\n"
        "Params iguales a `/dashboard/summary` (scope + id/name). "
        "Devuelve 202 con la lista de task_ids encolados. El frontend "
        "sigue polling `GET /dashboard/summary` — cuando "
        "`devices.sync_in_progress_count` vuelve a 0 los syncs terminaron."
    ),
)
def refresh_dashboard_scope(
    scope: str = Query(..., pattern="^(org|site|group|device)$"),
    id: Optional[int] = Query(default=None, ge=1),
    name: Optional[str] = Query(default=None),
    current_user: dict = Depends(require_authenticated),
    visibility_scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import dashboard_service
    from app.tasks import sync_device_task

    # Misma validación que el summary — mantener contrato consistente.
    if scope in ("site", "group") and id is None:
        raise ValidationError(f"scope={scope} requires 'id' query param")
    if scope == "device" and not name:
        raise ValidationError("scope=device requires 'name' query param")
    if scope == "org" and (id is not None or name is not None):
        raise ValidationError(
            "scope=org does not accept 'id' or 'name' params",
        )

    resuelto = dashboard_service.resolver_devices_del_scope(
        scope, id, name, visibility_scope,
    )
    if resuelto is None:
        target = f"{scope}={id or name}"
        raise NotFoundError(f"Scope target '{target}' not found")

    device_names, _ = resuelto

    # Política B (misma que summary): si el user no ve devices dentro del
    # scope, devolvemos 202 con 0 tareas encoladas en vez de 403. Deja
    # el frontend deshabilitar el botón basado en devices.total del
    # summary si prefiere UX más estricta.
    tareas: list[dict] = []
    for device_name in sorted(device_names):
        for sync_scope in _SYNC_SCOPES:
            result = sync_device_task.delay(device_name, sync_scope)
            tareas.append({
                "device": device_name,
                "scope": sync_scope,
                "task_id": result.id,
            })

    return ok({
        "devices_queued": len(device_names),
        "tasks_dispatched": len(tareas),
        "tasks": tareas,
    })
