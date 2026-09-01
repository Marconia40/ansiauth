from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.core.exceptions import DefaultGroupImmutableError


@dataclass
class DeviceGroup:
    """Grupo de devices dentro de un ``Site`` — Fase 6, A2.

    El campo que bloquea mutación es ``es_default`` (D7) — no la cantidad
    de devices que contiene (``FINAL_ARCHITECTURE.md`` decía lo contrario,
    corregido antes de escribir esta clase, ver ``FASE_6.md`` intro).

    ``created_at`` agregado -- mismo gap real que ``Site`` ya tuvo (ver
    ``app/models/site.py``, encontrado ahí comparando contra
    ``frontend/src/types/site.ts``) pero que el snippet canónico de A2 no
    tenía: ``frontend/src/services/api.ts: DeviceGroup`` declara
    ``created_at: string`` no-opcional -- sin este campo, cualquier
    response que arme un ``DeviceGroupRead`` real rompe.
    """

    id: int
    name: str
    description: Optional[str] = None
    site_id: Optional[int] = None
    es_default: bool = False
    created_at: Optional[datetime] = None

    def repositorio(self) -> str:
        return "device_group"

    def renombrar(self, nuevo_nombre: str) -> None:
        if self.es_default:
            raise DefaultGroupImmutableError(
                f"El grupo {self.id} es el Default del site y no se puede renombrar (D7)"
            )
        self.name = nuevo_nombre
