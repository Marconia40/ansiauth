from app.models.audit import AuditRecord
from app.models.domain_event import DomainEvent
from app.services.event_listener import EventListener


class AuditListener(EventListener):
    def __init__(self, audit_repo):  # AuditRepository, Línea B — FASE_3.md B1
        self._audit_repo = audit_repo

    def on_event(self, evento: DomainEvent) -> None:
        self._audit_repo.append(AuditRecord.desde(evento))
