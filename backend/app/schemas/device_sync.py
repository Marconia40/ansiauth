"""Envelope Pydantic para lecturas cache-first de recursos de device
(VLANs, ports, y futuros getters como interfaz virtual o config global).

Los GET que antes iban en vivo al equipo ahora leen de las tablas
``device_vlans`` / ``device_ports`` populated por ``sync_device_task``.
Para que el frontend sepa cuán vieja es esa data (y si vale la pena
mostrar un banner "última sync hace X min, refrescar"), la respuesta
viaja envuelta en este ``SyncedResource``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class SyncedResource(BaseModel):
    """Envelope estándar para respuestas cache-first.

    ``data`` es intencionalmente ``Any``: distintos recursos tienen
    shapes distintos (VLAN devuelve ``list[{vlan_id, name}]``, ports
    devuelve ``{device, vendor, count, ports:[...]}``). El envelope
    aporta la metadata de freshness, no impone un shape sobre la data
    misma -- así se reusa para futuros recursos sin subclasear.
    """

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "data": [{"vlan_id": 100, "name": "MGMT"}],
            "synced_at": "2026-09-01T12:34:56+00:00",
            "sync_error": None,
            "sync_in_progress": False,
        }
    })

    data: Any = Field(
        ...,
        description="Payload del recurso (shape depende del endpoint).",
    )
    synced_at: Optional[datetime] = Field(
        None,
        description=(
            "Timestamp UTC del último sync exitoso contra el equipo. "
            "``null`` si nunca se sincronizó (ej. device recién dado de "
            "alta cuya task todavía está corriendo, o refresh nunca "
            "disparado)."
        ),
    )
    sync_error: Optional[str] = Field(
        None,
        description=(
            "Mensaje del último intento de sync si falló. La ``data`` "
            "mostrada corresponde al último sync exitoso previo (nunca "
            "se vacía la cache al fallar). ``null`` si el último sync "
            "fue OK o nunca falló."
        ),
    )
    sync_in_progress: bool = Field(
        False,
        description=(
            "``true`` si hay una operación en curso sobre el device en "
            "este instante (sync o escritura). Se deriva del lock Redis "
            "del device, así que también captura escrituras concurrentes "
            "-- eso es intencional: mientras haya algo tocando al equipo "
            "no se puede confiar 100% en que la cache refleje lo que "
            "está por escribirse."
        ),
    )
