from abc import ABC, abstractmethod

from app.models.domain_event import DomainEvent


class EventListener(ABC):
    @abstractmethod
    def on_event(self, evento: DomainEvent) -> None: ...
