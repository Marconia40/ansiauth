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


@router.post(
    "/refresh",
    status_code=202,
    summary="Reactive refresh: sync only stale devices in a scope",
    description=(
        "Encola una sync de VLANs + ports + SVIs (scope ``all``) para "
        "los devices del scope cuyo último sync es más viejo que "
        "``stale_threshold_s`` (default 420s = 7 min). Devices frescos "
        "se omiten silenciosamente. Si un device ya tiene una sync "
        "pending (coalescing), tampoco se re-encola.\n\n"
        "El frontend llama este endpoint al abrir el dashboard (refresh "
        "reactivo). La carga sostenida del inventory la resuelve "
        "``sync_stale_devices_task`` (Celery Beat, cada ~20 min); este "
        "endpoint es sólo el disparador on-open que evita mostrar data "
        "vieja cuando el usuario vuelve al dashboard tras un rato.\n\n"
        "Params iguales a ``/dashboard/summary`` (scope + id/name). "
        "Devuelve 202 con lista de task_ids encolados. El frontend "
        "sigue polling ``GET /dashboard/summary`` — cuando "
        "``devices.sync_in_progress_count`` vuelve a 0 los syncs "
        "terminaron."
    ),
)
def refresh_dashboard_scope(
    scope: str = Query(..., pattern="^(org|site|group|device)$"),
    id: Optional[int] = Query(default=None, ge=1),
    name: Optional[str] = Query(default=None),
    stale_threshold_s: int = Query(
        default=420,
        ge=0,
        le=86400,
        description="Un device se considera 'stale' si su último sync es "
                    "más viejo que este umbral en segundos (0 fuerza refresh).",
    ),
    current_user: dict = Depends(require_authenticated),
    visibility_scope: VisibilityScope = Depends(obtener_scope),
):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, or_

    from app.composition import dashboard_service, redis_coordinator
    from app.db.models import DeviceModel
    from app.db.session import get_session
    from app.tasks import _encolar_sync_si_no_pendiente

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
    if not device_names:
        return ok({"devices_queued": 0, "tasks_dispatched": 0, "tasks": []})

    # Filtro de staleness: para cada device, el "último sync" es el MÁS
    # VIEJO de los 3 core scopes (mismo criterio que
    # DashboardService._resumen_devices). Si al menos uno es NULL, el
    # device se considera stale.
    threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_threshold_s)
    with get_session() as session:
        stale_rows = (
            session.query(DeviceModel.name)
            .filter(DeviceModel.name.in_(device_names))
            .filter(
                or_(
                    DeviceModel.vlans_synced_at.is_(None),
                    DeviceModel.ports_synced_at.is_(None),
                    DeviceModel.svis_synced_at.is_(None),
                    DeviceModel.vlans_synced_at < threshold,
                    DeviceModel.ports_synced_at < threshold,
                    DeviceModel.svis_synced_at < threshold,
                )
            )
            .order_by(DeviceModel.name.asc())
            .all()
        )
    stale_names = [n for (n,) in stale_rows]

    # Encolar sólo los stale, con coalescing (skip si ya hay pending).
    encolados = 0
    for device_name in stale_names:
        if _encolar_sync_si_no_pendiente(device_name, "all"):
            encolados += 1

    return ok({
        "devices_queued": encolados,
        "devices_skipped_fresh": len(device_names) - len(stale_names),
        "devices_skipped_coalesced": len(stale_names) - encolados,
        "tasks_dispatched": encolados,
    })
