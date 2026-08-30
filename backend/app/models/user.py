from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@dataclass
class User:
    """Cuenta de usuario -- entidad, faltaba en app/models/ (FINAL_ARCHITECTURE.md
    §1 la cataloga como `Usuario`, pero ninguna de las 7 fases de
    docs/migracion-final-architecture/ la asignó -- AutenticacionService/
    RefreshToken están explícitamente fuera de alcance de todo ese plan
    (README.md, "Alcance"), y User quedaba en el mismo cluster aunque no
    dependa de JWT/refresh tokens en sí. Agregada a pedido explícito, fuera
    de las 7 fases -- mismo patrón de entidad que Device/Job/Site
    (dataclass, hashing de password adentro en vez de un PasswordHasher
    inyectado -- CryptContext no necesita ningún secreto en runtime, a
    diferencia de SecretVault, así que no hace falta inyección por
    constructor acá).
    """

    id: Optional[int]
    username: str
    hashed_password: str
    email: Optional[str] = None
    is_active: bool = True
    is_system_admin: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @classmethod
    def nuevo(
        cls, username: str, password: str, email: str | None = None,
        is_system_admin: bool = False,
    ) -> "User":
        """Reemplaza user_service.create_user()'s normalización + hashing --
        mismo criterio que Device.nuevo(): valida/normaliza y devuelve la
        entidad lista para persistir, el chequeo de unicidad (username/email
        ya tomados) vive en UserRepository.crear() (necesita leer la DB,
        no puede vivir en un dataclass puro)."""
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        return cls(
            id=None,
            username=username.lower(),
            hashed_password=_pwd_context.hash(password),
            email=email.lower() if email else None,
            is_active=True,
            is_system_admin=bool(is_system_admin),
        )

    def verificar_password(self, plain_password: str) -> bool:
        """Reemplaza user_service.verify_password()/authenticate()'s mitad
        de verificación -- cuentas inactivas nunca autentican, mismo
        criterio que el real."""
        if not self.is_active:
            return False
        return _pwd_context.verify(plain_password, self.hashed_password)

    def cambiar_password(self, new_password: str) -> None:
        if len(new_password) < 8:
            raise ValueError("Password must be at least 8 characters")
        self.hashed_password = _pwd_context.hash(new_password)

    def actualizar(self, *, email: str | None = None, password: str | None = None) -> None:
        """Reemplaza user_service.update_user()'s parte de campos mutables --
        `is_active`/`is_system_admin` no entran acá a propósito: activar/
        desactivar y el toggle de system-admin son transiciones con guardas
        propias (último admin activo, D26), no un set-de-campo genérico --
        quedan como desactivar()/activar() separados, mismo criterio que
        Job._transicionar() vs. sus setters de campo simple."""
        if email is not None:
            self.email = email.lower()
        if password is not None:
            self.cambiar_password(password)

    def desactivar(self) -> None:
        self.is_active = False

    def activar(self) -> None:
        self.is_active = True
