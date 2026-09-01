from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainEvent:
    tipo: str
    recurso: Any
    device: Any
    actor: str
    payload: dict = field(default_factory=dict)
    exitoso: bool = True
