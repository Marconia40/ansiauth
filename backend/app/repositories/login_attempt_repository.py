"""LoginAttemptRepository — the one repository in the plan that does
NOT inherit from Repository[T].

Criterion (FASE_3.md §B2): a login attempt is never treated as an
entity with individual identity — no code path ever asks "give me
attempt #47", adds one by domain object, or removes one by id. Every
real operation is a windowed count, an insert-and-forget, or a bulk
delete. Inheriting a get/add/list/remove contract for zero real callers
would be noise, not scaffolding.

Reemplaza las 6 funciones sueltas que tenía login_attempt_service.py
(borrado — este repo era la única "dueña" real de la tabla desde que se
escribió, pero api/auth.py seguía llamando al módulo viejo hasta ahora,
FINAL_ARCHITECTURE.md §6): las dos variantes idénticas "borrar los
intentos fallidos de un username" (reset_username_failures, que no
devolvía nada, vs. unlock_username, que devolvía la cantidad) colapsan en
resetear(username) -> int, con la firma más informativa de las dos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import LoginAttemptModel
from app.db.session import get_session

USERNAME_FAIL_LIMIT: int = 5
USERNAME_LOCKOUT_MINUTES: int = 15
IP_FAIL_LIMIT: int = 20
IP_LOCKOUT_HOURS: int = 1


class LoginAttemptRepository:
    def esta_bloqueado(self, username: str) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=USERNAME_LOCKOUT_MINUTES)
        with get_session() as session:
            count = (
                session.query(LoginAttemptModel)
                .filter(
                    LoginAttemptModel.username == username.lower(),
                    LoginAttemptModel.succeeded == False,  # noqa: E712
                    LoginAttemptModel.attempted_at >= cutoff,
                )
                .count()
            )
        return count >= USERNAME_FAIL_LIMIT

    def ip_bloqueada(self, ip_address: str) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=IP_LOCKOUT_HOURS)
        with get_session() as session:
            count = (
                session.query(LoginAttemptModel)
                .filter(
                    LoginAttemptModel.ip_address == ip_address,
                    LoginAttemptModel.succeeded == False,  # noqa: E712
                    LoginAttemptModel.attempted_at >= cutoff,
                )
                .count()
            )
        return count >= IP_FAIL_LIMIT

    def registrar_intento(self, username: str, ip_address: str, exitoso: bool) -> None:
        with get_session() as session:
            session.add(
                LoginAttemptModel(
                    username=username.lower(),
                    ip_address=ip_address,
                    attempted_at=datetime.now(timezone.utc),
                    succeeded=exitoso,
                )
            )

    def purgar_antiguos(self, dias: int = 7) -> int:
        """Copia cleanup_service.py: sweep_old_login_attempts() tal cual --
        FASE_5.md B1, CleanupScheduler.limpiar_intentos_login() delega acá
        en vez de tocar LoginAttemptModel directo. La ventana de bloqueo
        real (USERNAME_LOCKOUT_MINUTES/IP_LOCKOUT_HOURS arriba) es mucho
        más corta que *dias* -- este purge solo afecta filas históricas que
        ya no influyen en ninguna decisión de lockout."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=dias)
        with get_session() as session:
            deleted = (
                session.query(LoginAttemptModel)
                .filter(LoginAttemptModel.attempted_at < cutoff)
                .delete(synchronize_session=False)
            )
        return deleted

    def resetear(self, username: str) -> int:
        """Delete every failed attempt for *username*. Returns the number
        of rows deleted — same shape as the old unlock_username, which
        callers of both reset_username_failures/unlock_username can
        adopt without behavior change (the discarded return is harmless)."""
        with get_session() as session:
            return (
                session.query(LoginAttemptModel)
                .filter(
                    LoginAttemptModel.username == username.lower(),
                    LoginAttemptModel.succeeded == False,  # noqa: E712
                )
                .delete(synchronize_session=False)
            )

    def resetear_todo(self) -> None:
        """Borra toda la tabla -- reemplaza login_attempt_service.reset_all(),
        usado solo en tests (conftest.py) para aislar cada test del estado
        de lockout que dejó el anterior."""
        with get_session() as session:
            session.query(LoginAttemptModel).delete(synchronize_session=False)
