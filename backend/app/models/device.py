from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.services.vendors.base import VendorDriver

# Duplicado hoy en device_service.py y inventory_service.py -- Fase 6 (A5)
# lo centraliza acá, el único lugar que le queda una vez que Inventory deja
# de tener su propio chequeo suelto.
#
# Debe coincidir exactamente con las claves que composition.py registra en
# PluginRegistry ("cisco_ios"/"huawei_vrp") -- bug real encontrado en una
# revisión de código: este set aceptaba "cisco"/"huawei" (sin driver
# registrado bajo esas claves -- Device.driver crasheaba con ValueError sin
# capturar, 500, la primera vez que se tocaba) y rechazaba "huawei_vrp" (la
# única clave que sí tiene un driver Huawei real). "cisco"/"huawei" ya no se
# aceptan -- si hace falta un alias corto en el futuro, tiene que traducirse
# a la clave real acá mismo, no vivir como 2 nombres que resuelven distinto
# según a qué capa se le pregunte.
_VALID_VENDORS = {"cisco_ios", "huawei_vrp"}

# "password" -- ansible_password de siempre, vía ansible_runner. "key" --
# bypasea ansible_runner por completo, ver VendorDriver._ejecutar() y el
# docstring de ssh_direct_service.py. Motivo real:
# contra f3r9s2 confirmamos en vivo que la autenticación por password tiene un
# fallo intermitente del lado del device ("User password authentication failed"
# con la password CORRECTA, sin patrón de concurrencia -- ver sesión de
# investigación) que la autenticación por clave no reprodujo en 20/20 intentos
# seguidos. Con un fleet de ~80 Huawei en mente, clave > password acá.
_VALID_AUTH_METHODS = {"password", "key"}


@dataclass
class Device:
    """Identidad de un device administrado (site, grupo, credenciales) —
    expone su driver de vendor y su password resueltos y cacheados, no
    ejecuta nada por sí mismo."""
    name: str
    host: str
    vendor: str
    username: str
    encrypted_password: str
    platform: str = "ios"
    # "password" (default, retrocompatible) o "key" -- ver _VALID_AUTH_METHODS.
    auth_method: str = "password"
    # Solo poblado cuando auth_method == "key". encrypted_password sigue
    # existiendo igual (vault.encrypt("") en ese caso) a propósito -- así
    # ninguno de los ~43 lugares que ya llaman device.password en
    # port.py/svi.py/vlan.py/global_config.py necesita saber que este device
    # usa clave; ese valor simplemente no se usa (ver VendorDriver._ejecutar()).
    encrypted_private_key: Optional[str] = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    site_id: Optional[int] = None
    site_name: Optional[str] = None
    # MSP: Phase 3 — the authoritative owning group. Populated by
    # ``device_service._to_domain`` from ``DeviceModel.device_group`` when
    # available; ``site_id``/``site_name`` above are derived from the group
    # when the MSP flag is on, and from the legacy row otherwise.
    device_group_id: Optional[int] = None
    device_group_name: Optional[str] = None

    # Lazily-resolved collaborators — not persisted, not part of __init__.
    _driver: "VendorDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _password: "str | None" = field(default=None, repr=False, compare=False, init=False)
    _private_key: "str | None" = field(default=None, repr=False, compare=False, init=False)

    @classmethod
    def nuevo(
        cls, name: str, host: str, vendor: str, platform: str, username: str,
        encrypted_password: str, device_group_id: int, *,
        auth_method: str = "password", encrypted_private_key: "str | None" = None,
    ) -> "Device":
        """Fábrica para Inventory.register() (Fase 6, A5) -- valida el vendor
        acá, en vez de un ``if vendor not in _VALID_VENDORS`` suelto en el
        caller (antes duplicado en device_service.py/inventory_service.py)."""
        if vendor not in _VALID_VENDORS:
            raise ValueError(
                f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}"
            )
        if auth_method not in _VALID_AUTH_METHODS:
            raise ValueError(
                f"auth_method '{auth_method}' not supported. Valid values: {', '.join(sorted(_VALID_AUTH_METHODS))}"
            )
        return cls(
            name=name, host=host, vendor=vendor, platform=platform,
            username=username, encrypted_password=encrypted_password,
            device_group_id=device_group_id, auth_method=auth_method,
            encrypted_private_key=encrypted_private_key,
        )

    @property
    def driver(self) -> "VendorDriver":
        if self._driver is None:
            from app.composition import plugin_registry  # import local -- ver FASE_1.md A3
            self._driver = plugin_registry.obtener(self.vendor)
        return self._driver

    @property
    def password(self) -> str:
        if self._password is None:
            from app.composition import secret_vault  # import local -- ver FASE_1.md A3
            self._password = secret_vault.decrypt(self.encrypted_password)
        return self._password

    @property
    def private_key(self) -> "str | None":
        """``None`` si ``auth_method != "key"`` o si nunca se cargó una
        clave -- mismo criterio de "lazy + cacheada" que ``password``."""
        if self.encrypted_private_key is None:
            return None
        if self._private_key is None:
            from app.composition import secret_vault  # import local -- ver FASE_1.md A3
            self._private_key = secret_vault.decrypt(self.encrypted_private_key)
        return self._private_key

    def actualizar(
        self, *, host: str | None = None, vendor: str | None = None,
        platform: str | None = None, username: str | None = None,
        password: str | None = None, auth_method: str | None = None,
        private_key: str | None = None, vault=None,
    ) -> None:
        """Reemplaza device_service.update_device() (Fase 6, A6) -- no encaja
        en ninguno de los 5 métodos de Inventory (no es create/list/get/move/
        delete), pasa a vivir acá. El grupo/site de un device se cambia vía
        POST /devices/{name}/move (Inventory.move()), nunca acá."""
        if host is not None:
            self.host = host
        if vendor is not None:
            if vendor not in _VALID_VENDORS:
                raise ValueError(
                    f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}"
                )
            self.vendor = vendor
        if platform is not None:
            self.platform = platform
        if username is not None:
            self.username = username
        if auth_method is not None:
            if auth_method not in _VALID_AUTH_METHODS:
                raise ValueError(
                    f"auth_method '{auth_method}' not supported. Valid values: {', '.join(sorted(_VALID_AUTH_METHODS))}"
                )
            self.auth_method = auth_method
        if password is not None:
            if vault is None:
                raise ValueError("actualizar(): password nuevo requiere vault")
            self.encrypted_password = vault.encrypt(password)
            self._password = None  # invalida el cache de Device.password
        if private_key is not None:
            if vault is None:
                raise ValueError("actualizar(): private_key nueva requiere vault")
            self.encrypted_private_key = vault.encrypt(private_key)
            self._private_key = None  # invalida el cache de Device.private_key
