"""UserRepository — reemplaza user_service.py, agregada fuera de las 7
fases de docs/migracion-final-architecture/ (User/AutenticacionService
quedaban explícitamente sin asignar, ver app/models/user.py).

`crear()`/`es_ultimo_admin_activo()` necesitan leer la DB antes de decidir
(unicidad de username/email; conteo de admins activos) -- no pueden vivir
en el dataclass puro `User`, mismo criterio que `SiteRepository.
crear_con_grupo_default()`/`tiene_devices()`.
"""
from __future__ import annotations

from typing import Optional

from app.core.repository import Repository
from app.db.models import UserModel
from app.db.session import get_session
from app.models.user import User


def _to_domain(row: UserModel) -> User:
    return User(
        id=row.id,
        username=row.username,
        hashed_password=row.hashed_password,
        email=row.email,
        is_active=row.is_active,
        is_system_admin=bool(row.is_system_admin),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_orm(u: User) -> UserModel:
    from datetime import datetime, timezone

    return UserModel(
        id=u.id,
        username=u.username,
        hashed_password=u.hashed_password,
        email=u.email,
        is_active=u.is_active,
        is_system_admin=u.is_system_admin,
        updated_at=datetime.now(timezone.utc),
    )


class UserRepository(Repository):
    def __init__(self):
        super().__init__(UserModel, _to_domain, _to_orm)

    def crear(
        self, username: str, password: str, email: Optional[str] = None,
        is_system_admin: bool = False,
    ) -> User:
        """Reemplaza user_service.create_user() -- chequeo de unicidad acá
        (necesita DB), construcción/hashing en User.nuevo()."""
        username_norm = username.lower()
        email_norm = email.lower() if email else None
        with get_session() as session:
            if session.query(UserModel).filter_by(username=username_norm).first():
                raise ValueError(f"Username '{username_norm}' is already taken")
            if email_norm and session.query(UserModel).filter_by(email=email_norm).first():
                raise ValueError(f"Email '{email_norm}' is already registered")
        entidad = User.nuevo(username, password, email, is_system_admin)
        return self.add(entidad)

    def obtener_por_username(self, username: str) -> Optional[User]:
        with get_session() as session:
            row = session.query(UserModel).filter_by(username=username.lower()).first()
            return _to_domain(row) if row else None

    def listar(self, *, incluir_inactivos: bool = False) -> list[User]:
        with get_session() as session:
            q = session.query(UserModel)
            if not incluir_inactivos:
                q = q.filter_by(is_active=True)
            rows = q.order_by(UserModel.created_at).all()
            return [_to_domain(r) for r in rows]

    def es_ultimo_admin_activo(self, user_id: int) -> bool:
        """Reemplaza user_service._is_last_active_system_admin() -- llamado
        antes de User.desactivar() para bloquear apagar el último
        system-admin activo (mismo guard real)."""
        with get_session() as session:
            row = session.query(UserModel).filter_by(id=user_id).first()
            if row is None or not row.is_system_admin:
                return False
            count = (
                session.query(UserModel)
                .filter_by(is_system_admin=True, is_active=True)
                .count()
            )
            return count <= 1
