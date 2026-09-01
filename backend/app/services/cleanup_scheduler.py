"""CleanupScheduler — reemplaza las 4 funciones sueltas de cleanup_service.py.
FASE_5.md B1.

``limpiar_intentos_login()`` delega a ``LoginAttemptRepository`` (única
dueña de la tabla, aunque no herede de ``Repository[T]``) en vez de tocar
``LoginAttemptModel`` directo. ``purgar_artefactos()``/``limpiar_refresh_tokens()``
no pasan por ningún Repository (filesystem el primero; no existe
``Repository[RefreshToken]`` en este plan el segundo — ver nota en
``limpiar_refresh_tokens()`` y `FASE_5.md` B1).
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from app.core.config import ANSIBLE_BASE_PATH
from app.db.models import RefreshTokenModel
from app.db.session import get_session

logger = logging.getLogger(__name__)


class CleanupScheduler:
    def __init__(self, login_attempt_repo):
        self._login_attempts = login_attempt_repo  # LoginAttemptRepository, Fase 3

    def purgar_artefactos(self, dias: int = 30) -> int:
        """Copia cleanup_service.py: purge_old_artifacts() tal cual --
        filesystem, sin DB, sin dependencia de ningún Repository."""
        artifacts_dir = os.path.join(ANSIBLE_BASE_PATH, "artifacts")
        if not os.path.isdir(artifacts_dir):
            logger.info("Artifact purge skipped: %s does not exist", artifacts_dir)
            return 0

        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=dias)).timestamp()
        removed = 0
        for entry in os.scandir(artifacts_dir):
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff_ts:
                    shutil.rmtree(entry.path)
                    removed += 1
            except OSError as exc:
                logger.warning("Failed to remove artifact %s: %s", entry.path, exc)

        logger.info(
            "Artifact purge complete: removed=%d retention_days=%d dir=%s",
            removed, dias, artifacts_dir,
        )
        return removed

    def limpiar_refresh_tokens(self) -> int:
        """Copia cleanup_service.py: sweep_expired_refresh_tokens() tal
        cual -- no existe Repository[RefreshToken] en este plan
        (AutenticacionService está fuera de alcance), excepción puntual y
        documentada a "el Repository es el único dueño del acceso a la DB"
        (FASE_5.md B1)."""
        now = datetime.now(timezone.utc)
        with get_session() as session:
            deleted = (
                session.query(RefreshTokenModel)
                .filter(RefreshTokenModel.expires_at < now)
                .delete(synchronize_session=False)
            )
        logger.info("Refresh-token sweep complete: deleted=%d", deleted)
        return deleted

    def limpiar_intentos_login(self, dias: int = 7) -> int:
        return self._login_attempts.purgar_antiguos(dias)

    def ejecutar_todo(self, artifact_retention_days: int = 30, login_attempt_retention_days: int = 7) -> dict:
        """Copia cleanup_service.py: run_all() tal cual -- try/except por
        rutina, una falla puntual (ej. permiso denegado en un artifact) no
        frena a las demás."""
        summary: dict = {}
        for name, func in (
            ("artifacts", lambda: self.purgar_artefactos(artifact_retention_days)),
            ("refresh_tokens", self.limpiar_refresh_tokens),
            ("login_attempts", lambda: self.limpiar_intentos_login(login_attempt_retention_days)),
        ):
            try:
                summary[name] = func()
            except Exception:
                logger.exception("Cleanup routine %r failed", name)
                summary[name] = None
        return summary
