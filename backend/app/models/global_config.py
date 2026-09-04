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
    app (RF-GLOBAL-08, 1 campo simple). ``snmp_config``: campo de acción
    tipo "struct" -- RF-GLOBAL-07 (versión/community/trap_source/trap_host)
    pide varios sub-campos juntos en la misma escritura. NTP/DNS/Log (antes
    agrupados en 1 solo ``log_servers_update``, SRS RF-GLOBAL-09) se
    separaron en 3 pares add/remove independientes por pedido explícito del
    usuario tras la primera vuelta -- cada uno se configura y borra por
    separado, y DNS/NTP/Log aceptan más de 1 server (mismo criterio
    incremental que ``SVI.dhcp_relay_add``). ``route_add``/``route_remove``
    son el mismo patrón para rutas estáticas.

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
    # -- escritura, acción puntual sobre una lista --
    route_add: dict | None = None
    route_remove: dict | None = None
    ntp_server_add: dict | None = None
    ntp_server_remove: dict | None = None
    dns_server_add: dict | None = None
    dns_server_remove: dict | None = None
    dns_domain_set: str | None = None
    log_server_add: dict | None = None
    log_server_remove: dict | None = None
    acl_upsert: dict | None = None
    # -- solo lectura, poblado por reconciliar()/el parser --
    running_config: str | None = None
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
        campos = (
            "hostname", "snmp_config", "route_add", "route_remove",
            "ntp_server_add", "ntp_server_remove", "dns_server_add", "dns_server_remove",
            "dns_domain_set", "log_server_add", "log_server_remove", "acl_upsert",
        )
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        """Reglas de escritura -- solo se llaman antes de ``aplicar()``,
        nunca durante ``reconciliar()``. Mismo criterio que
        ``SVI.validar()``."""
        if not self.mutation_fields:
            raise ValueError(
                "at least one mutation field must be provided "
                "(hostname, snmp_config, route_add, route_remove, ntp_server_add, "
                "ntp_server_remove, dns_server_add, dns_server_remove, dns_domain_set, "
                "log_server_add, log_server_remove, acl_upsert)"
            )
        if self.hostname is not None:
            _validate_hostname(self.hostname)
        if self.snmp_config is not None:
            claves_validas = {"version", "community", "trap_source", "trap_host", "trap_version"}
            desconocidas = set(self.snmp_config) - claves_validas
            if desconocidas:
                raise ValueError(f"snmp_config: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
            if not any(self.snmp_config.get(c) is not None for c in claves_validas):
                raise ValueError(f"snmp_config: at least one of {sorted(claves_validas)} must be provided")
            trap_host, trap_version = self.snmp_config.get("trap_host"), self.snmp_config.get("trap_version")
            if (trap_host is None) != (trap_version is None):
                raise ValueError("snmp_config: 'trap_host' and 'trap_version' must be provided together")
        if self.log_server_add is not None:
            desconocidas = set(self.log_server_add) - {"server", "level"}
            if desconocidas:
                raise ValueError(f"log_server_add: unknown keys {sorted(desconocidas)} (valid: ['server', 'level'])")
            if not self.log_server_add.get("server"):
                raise ValueError("log_server_add: 'server' is required")
        if self.log_server_remove is not None:
            desconocidas = set(self.log_server_remove) - {"server"}
            if desconocidas:
                raise ValueError(f"log_server_remove: unknown keys {sorted(desconocidas)} (valid: ['server'])")
            if not self.log_server_remove.get("server"):
                raise ValueError("log_server_remove: 'server' is required")
        if self.route_add is not None:
            self._validar_route_dict("route_add", self.route_add)
        if self.route_remove is not None:
            self._validar_route_dict("route_remove", self.route_remove)
        if self.ntp_server_add is not None:
            desconocidas = set(self.ntp_server_add) - {"server", "prefer"}
            if desconocidas:
                raise ValueError(f"ntp_server_add: unknown keys {sorted(desconocidas)} (valid: ['server', 'prefer'])")
            if not self.ntp_server_add.get("server"):
                raise ValueError("ntp_server_add: 'server' is required")
        if self.ntp_server_remove is not None:
            desconocidas = set(self.ntp_server_remove) - {"server"}
            if desconocidas:
                raise ValueError(f"ntp_server_remove: unknown keys {sorted(desconocidas)} (valid: ['server'])")
            if not self.ntp_server_remove.get("server"):
                raise ValueError("ntp_server_remove: 'server' is required")
        if self.dns_server_add is not None:
            desconocidas = set(self.dns_server_add) - {"server"}
            if desconocidas:
                raise ValueError(f"dns_server_add: unknown keys {sorted(desconocidas)} (valid: ['server'])")
            if not self.dns_server_add.get("server"):
                raise ValueError("dns_server_add: 'server' is required")
        if self.dns_server_remove is not None:
            desconocidas = set(self.dns_server_remove) - {"server"}
            if desconocidas:
                raise ValueError(f"dns_server_remove: unknown keys {sorted(desconocidas)} (valid: ['server'])")
            if not self.dns_server_remove.get("server"):
                raise ValueError("dns_server_remove: 'server' is required")
        if self.dns_domain_set is not None and not self.dns_domain_set.strip():
            raise ValueError("dns_domain_set: must not be empty")

    @staticmethod
    def _validar_route_dict(nombre_campo: str, valor: dict) -> None:
        """Shape compartido por ``route_add``/``route_remove`` -- ambos
        son ``{"destination": CIDR, "next_hop": IP}``, la única diferencia
        es qué hace ``aplicar()`` con eso."""
        claves_validas = {"destination", "next_hop"}
        desconocidas = set(valor) - claves_validas
        if desconocidas:
            raise ValueError(f"{nombre_campo}: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
        faltantes = claves_validas - {c for c in claves_validas if valor.get(c)}
        if faltantes:
            raise ValueError(f"{nombre_campo}: missing required keys {sorted(faltantes)}")
        try:
            ipaddress.ip_network(valor["destination"], strict=False)
        except ValueError as exc:
            raise ValueError(f"{nombre_campo}: invalid 'destination' ({exc})")
        try:
            ipaddress.ip_address(valor["next_hop"])
        except ValueError as exc:
            raise ValueError(f"{nombre_campo}: invalid 'next_hop' ({exc})")

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
        if campo == "route_add":
            return self._aplicar_route_add(device, pre_state)
        if campo == "route_remove":
            return self._aplicar_route_remove(device, pre_state)
        if campo == "ntp_server_add":
            return self._aplicar_ntp_add(device)
        if campo == "ntp_server_remove":
            return self._aplicar_ntp_remove(device)
        if campo == "dns_server_add":
            return self._aplicar_dns_add(device)
        if campo == "dns_server_remove":
            return self._aplicar_dns_remove(device)
        if campo == "dns_domain_set":
            return self._aplicar_dns_domain(device)
        if campo == "log_server_add":
            return self._aplicar_log_server_add(device)
        if campo == "log_server_remove":
            return self._aplicar_log_server_remove(device)
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
        """RF-GLOBAL-07. ``self.snmp_config`` puede traer ``version``,
        ``community`` (siempre RO, el permiso ya no es parámetro),
        ``trap_source`` y ``trap_host``+``trap_version`` -- solo se
        reenvían al driver los sub-campos que difieren del estado actual.
        ``trap_host`` arma un comando que además necesita la community (IOS
        ``snmp-server host {ip} version {v} {community}`` y el equivalente
        VRP la llevan en la misma línea) -- si no vino en esta misma
        request, se usa la ya conocida vía ``reconciliar()``; si no hay
        ninguna, error explícito (no tiene sentido armar un trap-host sin
        community resuelta)."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        cambios = {}
        version = self.snmp_config.get("version")
        if version is not None and version != (actual.snmp_version if actual is not None else None):
            cambios["version"] = version
        community = self.snmp_config.get("community")
        if community is not None and community != (actual.snmp_community if actual is not None else None):
            cambios["community"] = community
        trap_source = self.snmp_config.get("trap_source")
        if trap_source is not None:
            cambios["trap_source"] = trap_source
        trap_host, trap_version = self.snmp_config.get("trap_host"), self.snmp_config.get("trap_version")
        if trap_host is not None:
            community_para_trap = community or (actual.snmp_community if actual is not None else None)
            if not community_para_trap:
                raise ValueError(
                    "snmp_config: 'trap_host' requires a resolvable community "
                    "(either in this same request or already configured on the device)"
                )
            cambios["trap_host"] = trap_host
            cambios["trap_version"] = trap_version
            cambios["trap_host_community"] = community_para_trap
        if not cambios:
            return self._noop_resultado("configurar_snmp")
        resultado = device.driver.set_snmp(cambios, device, device.password)
        return {**resultado, "accion": "configurar_snmp"}

    def _aplicar_log_server_add(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (Log, split). Sin no-op detection, mismo criterio
        que NTP/DNS."""
        resultado = device.driver.add_log_server(
            self.log_server_add["server"], self.log_server_add.get("level"), device, device.password,
        )
        return {**resultado, "accion": "agregar_log_server"}

    def _aplicar_log_server_remove(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (Log, split, delete)."""
        resultado = device.driver.remove_log_server(self.log_server_remove["server"], device, device.password)
        return {**resultado, "accion": "eliminar_log_server"}

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

    def _aplicar_route_remove(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-06 (delete). Si la ruta pedida (destino+next-hop
        exactos) no existe, es no-op -- borrar algo que ya no está no
        debería ser un error (mismo criterio de idempotencia que el resto
        de esta clase).

        Límite real encontrado en vivo contra f3r9s1: ``routes`` sale de
        ``show ip route``/``display ip routing-table`` (la RIB), no de
        ``show running-config``/``display current-configuration`` -- una
        ruta con next-hop no alcanzable en la red real del device queda
        en el config pero NUNCA se instala en la RIB, así que este método
        no la ve y reporta no-op aunque el device SÍ tenga la línea. No
        hay forma de detectar ese caso desde acá sin leer running-config
        completo y parsear rutas de ahí también -- fuera de alcance por
        ahora, documentado para no repetir la confusión."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        destino_normalizado = str(ipaddress.ip_network(self.route_remove["destination"], strict=False))
        next_hop = self.route_remove["next_hop"]
        rutas_actuales = (actual.routes if actual is not None else None) or []
        existente = next(
            (r for r in rutas_actuales if r.get("destination") == destino_normalizado and r.get("next_hop") == next_hop),
            None,
        )
        if existente is None:
            return self._noop_resultado("eliminar_ruta")
        resultado = device.driver.remove_route(destino_normalizado, next_hop, device, device.password)
        return {**resultado, "accion": "eliminar_ruta"}

    def _aplicar_ntp_add(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (NTP, split). Sin no-op detection -- no hay lectura
        de NTP servers implementada (mismo gap ya documentado para SNMP
        community en Huawei), se manda el comando directo."""
        resultado = device.driver.add_ntp_server(
            self.ntp_server_add["server"], self.ntp_server_add.get("prefer") or False, device, device.password,
        )
        return {**resultado, "accion": "agregar_ntp"}

    def _aplicar_ntp_remove(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (NTP, split, delete). Mismo criterio sin no-op
        detection que ``_aplicar_ntp_add``."""
        resultado = device.driver.remove_ntp_server(self.ntp_server_remove["server"], device, device.password)
        return {**resultado, "accion": "eliminar_ntp"}

    def _aplicar_dns_add(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (DNS, split). Sin no-op detection, mismo criterio
        que NTP (no hay lectura de DNS servers implementada)."""
        resultado = device.driver.add_dns_server(self.dns_server_add["server"], device, device.password)
        return {**resultado, "accion": "agregar_dns"}

    def _aplicar_dns_remove(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (DNS, split, delete)."""
        resultado = device.driver.remove_dns_server(self.dns_server_remove["server"], device, device.password)
        return {**resultado, "accion": "eliminar_dns"}

    def _aplicar_dns_domain(self, device: "Device") -> dict:
        """RF-GLOBAL-09 (DNS, domain-name). Campo simple tipo "set", mismo
        criterio que ``hostname`` -- sin no-op detection porque tampoco hay
        lectura de domain-name implementada."""
        resultado = device.driver.set_dns_domain(self.dns_domain_set, device, device.password)
        return {**resultado, "accion": "configurar_dns_domain"}

    def repositorio(self) -> str:
        return "global_config"

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "hostname": self.hostname,
            "running_config": self.running_config,
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
