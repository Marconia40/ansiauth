import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.core.exceptions import TransicionInvalidaError

_TRANSICIONES_VALIDAS: dict[str, set[str]] = {
    "pending":   {"running", "cancelled"},
    "running":   {"completed", "failed", "cancelled"},
    "completed": set(),  # terminal
    "failed":    set(),  # terminal
    "cancelled": set(),  # terminal
}


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    operation: Optional[str] = None
    playbook: Optional[str] = None
    device: Optional[str] = None
    parameters: Optional[dict] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3
    rollback_performed: bool = False
    rollback_success: Optional[bool] = None
    pre_state: Optional[dict] = None
    last_error: Optional[str] = None
    current_step: Optional[str] = None
    group_job_id: Optional[str] = None

    def _transicionar(self, nuevo_status: str) -> None:
        validas = _TRANSICIONES_VALIDAS.get(self.status, set())
        if nuevo_status not in validas:
            raise TransicionInvalidaError(
                f"Job {self.job_id}: transición inválida {self.status!r} -> {nuevo_status!r}"
            )
        self.status = nuevo_status

    def esta_en_estado_terminal(self) -> bool:
        return not _TRANSICIONES_VALIDAS.get(self.status, {"__nunca__"})  # set vacío = terminal

    def marcar_iniciado(self) -> None:
        self._transicionar("running")
        self.started_at = datetime.now(timezone.utc)
        self.current_step = "executing"

    def marcar_completado(self, resultado: dict) -> None:
        self._transicionar("completed")
        self.result = resultado
        self.finished_at = datetime.now(timezone.utc)
        self.current_step = "completed"

    def marcar_fallido(self, error: str, rollback_performed: bool = False, rollback_success: Optional[bool] = None) -> None:
        self._transicionar("failed")
        self.error = error
        self.rollback_performed = rollback_performed
        self.rollback_success = rollback_success
        self.finished_at = datetime.now(timezone.utc)
        self.current_step = "failed"

    def cancelar(self) -> None:
        self._transicionar("cancelled")
        self.finished_at = datetime.now(timezone.utc)

    def registrar_reintento(self, error: str) -> None:
        """Reemplaza job_service.py: update_job(job_id, retry_count=...,
        last_error=..., current_step="retrying") -- llamada dentro del loop
        de reintentos, no al final. *error* es el texto del intento que
        acaba de fallar (stderr+stdout combinados), no ``self.error``
        (ese campo recién existe si el Job termina en failed -- durante un
        reintento en curso todavía no está seteado; usarlo acá copiaría
        ``None``, corrección real encontrada en Fase 5 armando
        `Orquestador._ejecutar_con_retry()`)."""
        self.retry_count += 1
        self.last_error = error.strip()[:500]
        self.current_step = "retrying"

    def asegurar_estado_final(self) -> None:
        """Única excepción deliberada -- NO llama a _transicionar(). Recuperación
        ante crash (server restart con jobs en 'running'), no una transición de
        negocio -- forzar 'running'->'failed' violaría la tabla de arriba a
        propósito, es exactamente el caso que esta excepción cubre."""
        if not self.esta_en_estado_terminal():
            self.status = "failed"
            if not self.error:
                self.error = "Unexpected termination"
            self.finished_at = datetime.now(timezone.utc)
