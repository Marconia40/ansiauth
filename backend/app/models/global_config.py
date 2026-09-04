from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device

# RF-GLOBAL-08 "Requires: Hostname válido (sin caracteres prohibidos)" --
# el SRS no dice cuáles son los caracteres prohibidos puntualmente. Alfa-
# numérico + guión, sin empezar/terminar en guión, es el subconjunto que
# tanto IOS como VRP aceptan sin ambigüedad (ambos rechazan espacios y la
# mayoría de los símbolos) -- confirmar contra un device real si hace
# falta un charset más permisivo.
_HOSTNAME_VALID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _validate_hostname(hostname: str) -> None:
    if not _HOSTNAME_VALID_RE.match(hostname):
        raise ValueError(
            "Hostname must be alphanumeric (hyphens allowed in the middle), "
            "1-63 characters, and not start or end with a hyphen"
        )


@dataclass
class GlobalConfig:
    """Domain representation de "Configuración Global" (SRS §3.4,
    RF-GLOBAL-01 a 09). Implementa el contrato ``RecursoGestionable``
    (``validar``/``reconciliar``/``aplicar``/``repositorio``), mismo patrón
    que ``VLAN``/``Puerto``/``SVI``.

    Identidad
    ---------
    A diferencia de VLAN/Puerto/SVI, acá la identidad ES el device solo --
    no hay colección de sub-elementos con su propio ID (1 fila por device,
    PK simple en vez de compuesta). ``routes``/``acls`` son listas (JSON)
    dentro de esa misma fila, no tablas propias -- ver docstring de
    ``DeviceGlobalConfigModel`` (``db/models.py``) para la justificación.

    Campos mutables
    ----------------
    ``hostname``: ``None`` = no tocar, mismo convenio que el resto de esta
    app (RF-GLOBAL-08, 1 campo simple). ``snmp_config``/
    ``log_servers_update``: campos de acción tipo "struct" -- RF-GLOBAL-07
    (versión+community+permiso) y RF-GLOBAL-09 (NTP+DNS+Log, el SRS los
    agrupa en 1 solo use case) piden varios sub-campos juntos en la misma
    escritura, así que van como 1 solo campo de mutación cada uno (dict),
    no 3-4 campos sueltos que forzarían 1 request por sub-campo. Dentro del
    dict, ``None``/ausente en una key = no tocar esa key puntual.
    ``route_add``/``acl_upsert`` son campos de acción puntual sobre una
    lista (agregan o reemplazan 1 elemento, no full-replace de toda la
    lista) -- mismo criterio que ``SVI.dhcp_relay_add``.

    Los campos de solo-lectura con el mismo nombre base (``snmp_version``,
    ``ntp_server``, etc.) muestran el último valor conocido -- poblados por
    ``reconciliar()``/el parser cuando existe lectura implementada para
    ellos (SNMP sí, ver RF-GLOBAL-02; NTP/DNS/Log el SRS no pide "consultar"
    aparte, quedan sin poblar hasta que haga falta)."""

    device: str = ""
    # -- escritura, campo simple --
    hostname: str | None = None
    # -- escritura, acción tipo struct (dict con sub-campos opcionales) --
    snmp_config: dict | None = None
    log_servers_update: dict | None = None
    # -- escritura, acción puntual sobre una lista --
    route_add: dict | None = None
    acl_upsert: dict | None = None
    # -- solo lectura, poblado por reconciliar()/el parser --
    device_version: str | None = None
    snmp_enabled: bool | None = None
    snmp_version: str | None = None
    snmp_community: str | None = None
    snmp_permission: str | None = None
    ntp_server: str | None = None
    dns_server: str | None = None
    log_server: str | None = None
    log_level: str | None = None
    routes: list[dict] | None = None
    acls: list[str] | None = None

    @property
    def mutation_fields(self) -> set[str]:
        campos = ("hostname", "snmp_config", "log_servers_update", "route_add", "acl_upsert")
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        """Reglas de escritura -- solo se llaman antes de ``aplicar()``,
        nunca durante ``reconciliar()``. Mismo criterio que
        ``SVI.validar()``."""
        if not self.mutation_fields:
            raise ValueError(
                "at least one mutation field must be provided "
                "(hostname, snmp_config, log_servers_update, route_add, acl_upsert)"
            )
        if self.hostname is not None:
            _validate_hostname(self.hostname)
        if self.snmp_config is not None:
            claves_validas = {"version", "community", "permission"}
            desconocidas = set(self.snmp_config) - claves_validas
            if desconocidas:
                raise ValueError(f"snmp_config: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
            if not any(self.snmp_config.get(c) is not None for c in claves_validas):
                raise ValueError("snmp_config: at least one of version, community, permission must be provided")
            community, permission = self.snmp_config.get("community"), self.snmp_config.get("permission")
            if (community is None) != (permission is None):
                raise ValueError(
                    "snmp_config: 'community' and 'permission' must be provided together "
                    "(both vendors' commands set them in a single line)"
                )
            if permission is not None and permission not in ("RO", "RW"):
                raise ValueError("snmp_config: permission must be 'RO' or 'RW'")
        if self.log_servers_update is not None:
            claves_validas = {"ntp_server", "dns_server", "log_server", "log_level"}
            desconocidas = set(self.log_servers_update) - claves_validas
            if desconocidas:
                raise ValueError(f"log_servers_update: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
            if not any(self.log_servers_update.get(c) is not None for c in claves_validas):
                raise ValueError("log_servers_update: at least one of ntp_server, dns_server, log_server, log_level must be provided")
            if self.log_servers_update.get("log_level") is not None and self.log_servers_update.get("log_server") is None:
                raise ValueError(
                    "log_servers_update: 'log_level' requires 'log_server' in the same request "
                    "(VRP ties the severity level to the loghost line itself)"
                )
        if self.route_add is not None:
            claves_validas = {"destination", "next_hop"}
            desconocidas = set(self.route_add) - claves_validas
            if desconocidas:
                raise ValueError(f"route_add: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
            faltantes = claves_validas - {c for c in claves_validas if self.route_add.get(c)}
            if faltantes:
                raise ValueError(f"route_add: missing required keys {sorted(faltantes)}")
            try:
                ipaddress.ip_network(self.route_add["destination"], strict=False)
            except ValueError as exc:
                raise ValueError(f"route_add: invalid 'destination' ({exc})")
            try:
                ipaddress.ip_address(self.route_add["next_hop"])
            except ValueError as exc:
                raise ValueError(f"route_add: invalid 'next_hop' ({exc})")

    def reconciliar(self, device: "Device") -> dict:
        """Estado actual de la configuración global de *device*, leído en
        vivo -- mismo criterio que ``SVI.reconciliar()``: una escritura
        necesita el estado real del momento, no la cache que sirve
        ``GET /global-config``."""
        actual = device.driver.get_global_config(device, device.password)
        return {"existed": actual is not None, "actual": actual}

    def aplicar(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """Mismo criterio que ``SVI.aplicar()``: el dict devuelto siempre
        incluye "accion"."""
        campos = self.mutation_fields
        if len(campos) != 1:
            raise ValueError(
                f"GlobalConfig.aplicar(): se espera exactamente 1 campo de "
                f"mutación por llamada (se recibieron {sorted(campos)}) -- "
                f"un endpoint por campo, mismo criterio que SVI/Puerto"
            )
        campo = next(iter(campos))
        if campo == "hostname":
            return self._aplicar_hostname(device, pre_state)
        if campo == "snmp_config":
            return self._aplicar_snmp_config(device, pre_state)
        if campo == "log_servers_update":
            return self._aplicar_log_servers(device, pre_state)
        if campo == "route_add":
            return self._aplicar_route_add(device, pre_state)
        raise ValueError(f"GlobalConfig.aplicar(): no hay driver call para el campo {campo!r} todavía")

    def _noop_resultado(self, accion: str, **extra: object) -> dict:
        return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": accion, **extra}

    def _aplicar_hostname(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-08. Mismo criterio de no-op que ``SVI._aplicar_description()``:
        si el hostname pedido ya es el actual, no reenvía el comando."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        if actual is not None and actual.hostname == self.hostname:
            return self._noop_resultado("modificar_hostname")
        resultado = device.driver.set_hostname(self.hostname, device, device.password)
        return {**resultado, "accion": "modificar_hostname"}

    def _aplicar_snmp_config(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-07. ``self.snmp_config`` puede traer ``version``/
        ``community``/``permission`` -- solo se reenvían al driver los
        sub-campos que difieren del estado actual (mismo criterio de no-op
        que ``_aplicar_hostname``, aplicado campo por campo)."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        cambios = {}
        version = self.snmp_config.get("version")
        if version is not None and version != (actual.snmp_version if actual is not None else None):
            cambios["version"] = version
        community, permission = self.snmp_config.get("community"), self.snmp_config.get("permission")
        if community is not None:
            actual_community = actual.snmp_community if actual is not None else None
            actual_permission = actual.snmp_permission if actual is not None else None
            if community != actual_community or permission != actual_permission:
                cambios["community"], cambios["permission"] = community, permission
        if not cambios:
            return self._noop_resultado("configurar_snmp")
        resultado = device.driver.set_snmp(cambios, device, device.password)
        return {**resultado, "accion": "configurar_snmp"}

    def _aplicar_log_servers(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-09. ``self.log_servers_update`` puede traer
        ``ntp_server``/``dns_server``/``log_server``/``log_level`` -- mismo
        criterio de no-op por sub-campo que ``_aplicar_snmp_config``."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        cambios = {}
        for clave in ("ntp_server", "dns_server", "log_server", "log_level"):
            if clave not in self.log_servers_update or self.log_servers_update[clave] is None:
                continue
            valor_actual = getattr(actual, clave) if actual is not None else None
            if self.log_servers_update[clave] != valor_actual:
                cambios[clave] = self.log_servers_update[clave]
        if not cambios:
            return self._noop_resultado("configurar_log_servers")
        resultado = device.driver.set_log_servers(cambios, device, device.password)
        return {**resultado, "accion": "configurar_log_servers"}

    def _aplicar_route_add(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-06. "Alternative course: Ruta ya existente, se
        notifica al usuario" -- si ya hay una ruta con el mismo destino,
        NO se sobreescribe (aunque el next-hop pedido sea distinto):
        se devuelve ``accion="ruta_ya_existe"`` sin tocar el device. Solo
        si el destino coincide Y el next-hop también, es un no-op real
        (mismo criterio que el resto de esta clase)."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        destino_normalizado = str(ipaddress.ip_network(self.route_add["destination"], strict=False))
        next_hop = self.route_add["next_hop"]
        rutas_actuales = (actual.routes if actual is not None else None) or []
        existente = next((r for r in rutas_actuales if r.get("destination") == destino_normalizado), None)
        if existente is not None:
            if existente.get("next_hop") == next_hop:
                return self._noop_resultado("agregar_ruta")
            return {
                "rc": 0, "success": True, "changed": False, "accion": "ruta_ya_existe",
                "destination": destino_normalizado, "next_hop_actual": existente.get("next_hop"),
            }
        resultado = device.driver.set_route(destino_normalizado, next_hop, device, device.password)
        return {**resultado, "accion": "agregar_ruta"}

    def repositorio(self) -> str:
        return "global_config"

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "hostname": self.hostname,
            "device_version": self.device_version,
            "snmp_enabled": self.snmp_enabled,
            "snmp_version": self.snmp_version,
            "snmp_community": self.snmp_community,
            "snmp_permission": self.snmp_permission,
            "ntp_server": self.ntp_server,
            "dns_server": self.dns_server,
            "log_server": self.log_server,
            "log_level": self.log_level,
            "routes": self.routes,
            "acls": self.acls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GlobalConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
