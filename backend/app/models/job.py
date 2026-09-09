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
    # Frase corta y legible de la INTENCIÓN de este job (ej. "Add route
    # 192.168.100.0/24 -> 10.10.100.1"), calculada UNA vez por
    # GroupOperationRunner vía RecursoGestionable.resumen_intento() --
    # reemplaza mostrar `parameters` (el asdict() crudo del dataclass
    # completo, con la mayoría de sus campos en None) en el frontend.
    parameters_summary: Optional[str] = None
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
    # Clasificación amigable del error final (poblados por
    # ``Orquestador._error_amigable()`` al marcar el job como failed). El
    # ``error`` crudo se sigue guardando para debug -- estos 3 son el
    # resumen legible que ``GET /jobs/{id}`` devuelve al frontend sin que
    # haya que re-derivar la clasificación cada vez.
    #
    # - ``error_type``: "permanent" | "transient" | "unknown" (mismo eje que
    #   ``RetryDecision.classification``).
    # - ``error_reason``: patrón crudo que matcheó en las tablas de
    #   ``_clasificar_error`` (ej. "invalid input", "unable to open
    #   channel") -- útil para tunear patterns con datos reales.
    # - ``error_summary``: mensaje corto en inglés para mostrar en UI (ej.
    #   "Command not supported by this device"). Preferido sobre el
    #   ``error`` crudo para render.
    error_type: Optional[str] = None
    error_reason: Optional[str] = None
    error_summary: Optional[str] = None
    # Cuando además del apply falla el rollback (device rechaza el revert,
    # timeout, o el verify per-recurso no coincide con el pre_state), acá
    # queda el motivo crudo -- Orquestador._rollback_lote() lo captura y
    # el orquestador lo persiste antes de despachar el evento. Sin esto
    # el usuario veía ``rollback_success: false`` sin poder saber POR
    # QUÉ (device error / verify mismatch / timeout).
    rollback_error: Optional[str] = None
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

    def marcar_fallido(
        self,
        error: str,
        rollback_performed: bool = False,
        rollback_success: Optional[bool] = None,
        error_type: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_summary: Optional[str] = None,
    ) -> None:
        self._transicionar("failed")
        self.error = error
        self.rollback_performed = rollback_performed
        self.rollback_success = rollback_success
        self.error_type = error_type
        self.error_reason = error_reason
        self.error_summary = error_summary
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
