"""Schemas del endpoint de agregación para el Dashboard.

Reemplaza el patrón N+1 donde el frontend traía la lista completa de
devices + una query de VLANs/ports por cada uno. Con este summary el
dashboard hace 1 sola request y todo el cómputo (contar devices por
vendor, sumar ports up/down, agregar VLANs únicas, contar jobs por
estado) sucede server-side en 1-2 queries a la DB.

Discrepancia de nombre de VLAN: el backend NO calcula qué VLAN IDs
tienen nombres distintos entre devices; devuelve la lista de names
únicos por VLAN y el frontend detecta la discrepancia con
``names.length > 1``. Decisión de diseño para no duplicar la lógica
que hoy ya tiene el frontend.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.global_config import (
    GlobalConfigAclInfo,
    GlobalConfigDnsInfo,
    GlobalConfigLoggingInfo,
    GlobalConfigNtpInfo,
    GlobalConfigSnmpInfo,
)


class DashboardScope(BaseModel):
    """Identifica el scope agregado en la respuesta."""

    kind: str = Field(..., description="'org' | 'site' | 'group' | 'device'")
    id: Optional[int] = Field(None, description="Site o group ID (null para org y device)")
    name: Optional[str] = Field(None, description="Device name (solo para scope=device) o nombre del site/group para mostrar")


class DevicesSummary(BaseModel):
    total: int
    by_vendor: dict[str, int] = Field(
        ...,
        description="{vendor: count} — vendor keys se dejan abiertas por si mañana entra un tercer vendor sin romper el frontend.",
    )
    sync_errors: int = Field(
        ...,
        description="Cuántos devices tienen sync_error != null en al menos una de sus tres caches (vlans/ports/svis).",
    )
    last_sync_at: Optional[datetime] = Field(
        None,
        description="El synced_at MÁS VIEJO del scope, consolidando vlans/ports/svis. Null si ningún device sincronizó nunca.",
    )
    sync_in_progress_count: int = Field(
        ...,
        description="Cuántos devices tienen el lock Redis tomado en este instante. Si es >0, el frontend hace polling cada 2s.",
    )


class VlanSummaryEntry(BaseModel):
    id: int
    names: list[str] = Field(
        ...,
        description="Lista de nombres únicos observados para este VLAN ID en el scope. Si len > 1 hay discrepancia entre devices.",
    )


class VlansSummary(BaseModel):
    unique_count: int
    entries: list[VlanSummaryEntry] = Field(
        ...,
        description="Ordenadas por id ascendente. El frontend detecta discrepancia con entries[i].names.length > 1.",
    )


class PortsSummary(BaseModel):
    total: int
    up: int = Field(..., description="admin_up != false AND operational_up == true")
    down: int = Field(..., description="admin_up != false AND operational_up == false")
    shutdown: int = Field(..., description="admin_up == false (independiente de operational)")


class SvisSummary(BaseModel):
    """Mismo shape que Ports — el compañero pidió consistencia entre ambas cards."""

    total: int
    up: int = Field(..., description="admin_up != false AND operational_up == true")
    down: int = Field(..., description="admin_up != false AND operational_up == false")
    shutdown: int = Field(..., description="admin_up == false")


class JobsSummary(BaseModel):
    window_days: int = Field(..., description="Ventana temporal aplicada (default 7, configurable vía ?jobs_days=N).")
    total: int
    by_status: dict[str, int] = Field(
        ...,
        description="Contadores por status. Keys posibles: pending, running, completed, failed, cancelled.",
    )
    rollback_performed_count: int


class GlobalConfigScopeDeviceEntry(BaseModel):
    """1 device dentro de la sección opt-in ``global_config`` (ver
    ``?include_global_config=true``). Mismo shape que ``GlobalConfigRead``
    (`schemas/global_config.py`) más device/vendor/metadata de sync —
    reusa las mismas sub-schemas, no duplica el mapeo de campos."""

    device: str
    vendor: Optional[str] = None
    hostname: Optional[str] = None
    snmp: GlobalConfigSnmpInfo = Field(default_factory=GlobalConfigSnmpInfo)
    ntp: GlobalConfigNtpInfo = Field(default_factory=GlobalConfigNtpInfo)
    dns: GlobalConfigDnsInfo = Field(default_factory=GlobalConfigDnsInfo)
    logging: GlobalConfigLoggingInfo = Field(default_factory=GlobalConfigLoggingInfo)
    acls: Optional[list[GlobalConfigAclInfo]] = Field(
        None, description="ACLs configuradas en el device, con sus reglas, o null.",
    )
    synced_at: Optional[datetime] = Field(None, description="Último sync de global_config para este device, o null si nunca sincronizó.")
    sync_error: Optional[str] = Field(None, description="Último error de sync de global_config, o null.")
    sync_in_progress: bool = Field(..., description="True si hay un sync de global_config corriendo ahora mismo para este device.")


class DashboardSummaryResponse(BaseModel):
    """Payload que devuelve GET /api/v1/dashboard/summary."""

    scope: DashboardScope
    generated_at: datetime = Field(..., description="Timestamp UTC de cuándo se computó este summary (ahora).")
    devices: DevicesSummary
    vlans: VlansSummary
    ports: PortsSummary
    svis: SvisSummary
    jobs: JobsSummary
    global_config: Optional[list[GlobalConfigScopeDeviceEntry]] = Field(
        None,
        description=(
            "SNMP/NTP/DNS/logging/ACLs por device — solo presente si se pidió "
            "`?include_global_config=true`. Null (no `[]`) cuando no se pidió, "
            "para que el frontend pueda distinguir 'no lo pedí' de 'scope vacío'."
        ),
    )
