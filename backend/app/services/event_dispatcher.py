from app.models.domain_event import DomainEvent
from app.services.event_listener import EventListener


class EventDispatcher:
    def __init__(self):
        self._listeners: list[EventListener] = []

    def suscribir(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def despachar(self, eventos: list[DomainEvent]) -> None:
        for evento in eventos:
            for listener in self._listeners:
                listener.on_event(evento)
