import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class AuditRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    user: str
    action: str
    resource: str
    resource_id: Optional[str] = None
    details: dict
    status: str = "success"
    job_id: Optional[str] = None
    device: Optional[str] = None
    request_id: Optional[str] = None
    parent_audit_id: Optional[str] = None

    @classmethod
    def desde(cls, evento: "DomainEvent") -> "AuditRecord":
        """Construye un AuditRecord a partir de un DomainEvent genérico —
        FASE_3.md A4. `evento.tipo` es genérico a propósito
        ("recurso_aplicado"/"recurso_fallido", Orquestador no sabe qué VLAN/
        Puerto es), pero `action` necesita ser específico (RF-AUD-02) — se lee
        de `payload["accion"]` (VLAN.aplicar()/Puerto.aplicar(), Fase 2, ya lo
        incluyen), con `evento.tipo` como fallback para eventos que no pasan
        por aplicar() (ej. los de Inventory, ya suficientemente específicos).
        `exitoso` viene explícito del evento, no se infiere de payload/tipo."""
        accion = evento.payload.get("accion", evento.tipo)
        return cls(
            timestamp=datetime.now(timezone.utc),
            user=evento.actor,
            action=accion,
            resource=evento.recurso.repositorio() if hasattr(evento.recurso, "repositorio") else type(evento.recurso).__name__.lower(),
            details=evento.payload,
            status="success" if evento.exitoso else "failure",
            device=getattr(evento.device, "name", None),
        )
