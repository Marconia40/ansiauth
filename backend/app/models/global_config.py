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
    ``ntp_servers``, etc.) se extraen del ``running_config`` -- poblados por
    ``reconciliar()``/el parser. ``ntp_servers``/``dns_servers``/
    ``log_servers`` son listas (puede haber más de 1 configurado, ej. varios
    ``ip name-server``) a diferencia de ``snmp_version``/``snmp_community``
    que son 1 solo valor. ``acls`` es una lista de dict (nombre/tipo/reglas
    de cada ACL), no solo nombres -- ver ``list_acls()`` en cada driver."""

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
    # RF-GLOBAL-05. ``acl_create``: {"name": str, "rules": list[rule-dict]}
    # -- crea la ACL si no existe, agrega las reglas si ya existe (mismo
    # comando sirve para ambos casos). ``acl_rule_remove``: mismo shape,
    # saca las reglas indicadas. ``acl_delete``: nombre de la ACL a borrar
    # completa. A diferencia de NTP/DNS/log (1 valor por request), acá se
    # acepta una LISTA de reglas -- una ACL real se puebla con muchas
    # reglas de una (ver ejemplo real del usuario, 18 reglas), armar 1
    # bloque de comando con todas es más barato que 1 request por regla.
    acl_create: dict | None = None
    acl_rule_remove: dict | None = None
    acl_delete: str | None = None
    # -- solo lectura, poblado por reconciliar()/el parser --
    running_config: str | None = None
    device_version: str | None = None
    snmp_enabled: bool | None = None
    snmp_version: str | None = None
    snmp_community: str | None = None
    snmp_permission: str | None = None
    snmp_trap_hosts: list[str] | None = None
    # Cisco: no aplica, siempre None (el trap_host es 1 comando explícito
    # sin ACL asociada, ver ``set_snmp()``). Huawei: nombre de la ACL atada
    # al agente vía "snmp-agent acl {nombre}" -- transiente, solo se usa
    # dentro de ``_aplicar_snmp_config()``/``set_snmp()`` para saber a qué
    # ACL agregarle una regla al escribir un nuevo trap_host (no se
    # persiste en cache/DB, se resuelve en vivo vía ``reconciliar()`` en
    # cada escritura, igual que el resto de las validaciones de no-op).
    snmp_acl_name: str | None = None
    ntp_servers: list[str] | None = None
    dns_servers: list[str] | None = None
    log_servers: list[str] | None = None
    log_level: str | None = None
    routes: list[dict] | None = None
    acls: list[dict] | None = None
    # ARP/MAC -- agregadas al cache/sync a pedido del usuario (eran la
    # única lectura 100% en vivo de toda la app, cada GET pagaba el
    # overhead completo de una sesión SSH/Ansible nueva). Se aceptan
    # "desactualizadas hasta el próximo sync" a cambio de que el GET sea
    # instantáneo -- mismo trade-off que el resto del cache de esta app.
    # Se leen SIEMPRE completas (sin ``include``) durante el sync -- el
    # filtro ``include`` de ``GET /arp``/``GET /mac`` ahora se aplica del
    # lado de la API sobre estos datos ya cacheados, no como un pipe en el
    # device (ver ``api/global_config.py``).
    arp_table: list[dict] | None = None
    mac_table: list[dict] | None = None

    @property
    def mutation_fields(self) -> set[str]:
        campos = (
            "hostname", "snmp_config", "route_add", "route_remove",
            "ntp_server_add", "ntp_server_remove", "dns_server_add", "dns_server_remove",
            "dns_domain_set", "log_server_add", "log_server_remove",
            "acl_create", "acl_rule_remove", "acl_delete",
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
                "log_server_add, log_server_remove, acl_create, acl_rule_remove, acl_delete)"
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
        if self.acl_create is not None:
            self._validar_acl_dict("acl_create", self.acl_create)
        if self.acl_rule_remove is not None:
            self._validar_acl_dict("acl_rule_remove", self.acl_rule_remove)
        if self.acl_delete is not None and not self.acl_delete.strip():
            raise ValueError("acl_delete: must not be empty")

    @staticmethod
    def _validar_acl_dict(nombre_campo: str, valor: dict) -> None:
        """Shape compartido por ``acl_create``/``acl_rule_remove`` --
        ambos son ``{"name": str, "rules": list[dict]}``. La forma de
        cada regla individual (action/protocol/source/destination/port)
        ya la valida el schema de Pydantic en la API antes de llegar
        acá -- mismo criterio que ``_validar_route_dict()``."""
        claves_validas = {"name", "rules"}
        desconocidas = set(valor) - claves_validas
        if desconocidas:
            raise ValueError(f"{nombre_campo}: unknown keys {sorted(desconocidas)} (valid: {sorted(claves_validas)})")
        if not valor.get("name"):
            raise ValueError(f"{nombre_campo}: 'name' is required")
        if not valor.get("rules"):
            raise ValueError(f"{nombre_campo}: 'rules' must be a non-empty list")

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
        ``GET /global-config``. ``incluir_arp_mac=False`` -- ninguna
        escritura necesita ese dato para su no-op detection, y son 2
        conexiones SSH menos en un camino que ya de por sí abre varias
        (confirmado en vivo contra huawei01, 5 líneas VTY: sin este ahorro,
        una escritura de ACL se quedaba sin sesiones -- "Channel
        closed")."""
        actual = device.driver.get_global_config(device, device.password, incluir_arp_mac=False)
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
        if campo == "acl_create":
            return self._aplicar_acl_create(device, pre_state)
        if campo == "acl_rule_remove":
            return self._aplicar_acl_rule_remove(device, pre_state)
        if campo == "acl_delete":
            return self._aplicar_acl_delete(device)
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
        ``trap_host`` se resuelve de 2 formas posibles según lo que
        ``reconciliar()`` haya encontrado en ``actual.snmp_acl_name``:
        - Si HAY una ACL atada al agente SNMP (Huawei con ``snmp-agent acl
          {nombre}`` configurado) -- se manda ``trap_host_acl_name`` al
          driver, que agrega una regla ``permit source`` a esa ACL (no
          necesita community, es una operación de ACL, no de SNMP en sí).
        - Si NO hay ACL (Cisco, o Huawei sin ninguna atada) -- arma un
          comando que necesita la community (IOS ``snmp-server host {ip}
          version {v} {community}`` y el equivalente VRP la llevan en la
          misma línea): si no vino en esta misma request, usa la ya
          conocida vía ``reconciliar()``; si no hay ninguna, error
          explícito."""
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
            acl_name = actual.snmp_acl_name if actual is not None else None
            if acl_name is not None:
                cambios["trap_host"] = trap_host
                cambios["trap_version"] = trap_version
                cambios["trap_host_acl_name"] = acl_name
            else:
                community_para_trap = community or (actual.snmp_community if actual is not None else None)
                if not community_para_trap:
                    raise ValueError(
                        "snmp_config: 'trap_host' requires a resolvable community "
                        "(either in this same request or already configured on the device), "
                        "unless the device already has an ACL bound to its SNMP agent"
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
        """RF-GLOBAL-09 (NTP, split). Sin no-op detection -- aunque
        ``ntp_servers`` ya se puede leer, comparar contra un alta
        incremental de a 1 no está implementado (fuera de alcance de esta
        vuelta), se manda el comando directo."""
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
        que NTP -- ``dns_servers`` ya se puede leer pero no se compara
        contra el alta incremental."""
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

    @staticmethod
    def _reglas_acl_actuales(actual: "GlobalConfig | None", name: str) -> list[str]:
        """Reglas crudas (con el prefijo de secuencia/número que agrega
        cada vendor al leerlas, ej. "10 permit ..."/"rule 5 permit ...")
        de la ACL *name* según el último ``reconciliar()``, o ``[]`` si la
        ACL no existe todavía."""
        if actual is None or not actual.acls:
            return []
        acl = next((a for a in actual.acls if a.get("name") == name), None)
        return acl.get("rules", []) if acl is not None else []

    @staticmethod
    def _regla_ya_presente(regla_formateada: str, reglas_actuales: list[str]) -> bool:
        """*regla_formateada* (sin prefijo de secuencia, la arma
        ``driver.formatear_regla_acl()``) está presente si alguna regla
        actual TERMINA con ese texto -- el prefijo que agrega cada vendor
        al leer (número de secuencia Cisco, "rule N" Huawei) siempre va
        ANTES del contenido real de la regla, nunca lo interrumpe."""
        return any(actual_line.endswith(regla_formateada) for actual_line in reglas_actuales)

    def _aplicar_acl_create(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-05. Crea la ACL si no existe, agrega las reglas
        nuevas si ya existe (mismo comando de driver sirve para ambos
        casos). No-op por regla -- las que ya estén configuradas tal cual
        no se re-envían; si TODAS ya están, no-op completo."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        name = self.acl_create["name"]
        reglas_actuales = self._reglas_acl_actuales(actual, name)
        rule_lines = [
            formateada for r in self.acl_create["rules"]
            if not self._regla_ya_presente(formateada := device.driver.formatear_regla_acl(r), reglas_actuales)
        ]
        if not rule_lines:
            return self._noop_resultado("crear_o_extender_acl")
        resultado = device.driver.create_or_update_acl(name, rule_lines, device, device.password)
        return {**resultado, "accion": "crear_o_extender_acl"}

    def _aplicar_acl_rule_remove(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-GLOBAL-05 (delete de reglas puntuales). Solo se mandan las
        reglas que SÍ están actualmente en la ACL -- sacar algo que no
        está no debería ser un error (mismo criterio de idempotencia que
        el resto de esta clase)."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        name = self.acl_rule_remove["name"]
        reglas_actuales = self._reglas_acl_actuales(actual, name)
        rule_lines = [
            formateada for r in self.acl_rule_remove["rules"]
            if self._regla_ya_presente(formateada := device.driver.formatear_regla_acl(r), reglas_actuales)
        ]
        if not rule_lines:
            return self._noop_resultado("sacar_reglas_acl")
        resultado = device.driver.remove_acl_rules(name, rule_lines, device, device.password)
        return {**resultado, "accion": "sacar_reglas_acl"}

    def _aplicar_acl_delete(self, device: "Device") -> dict:
        """RF-GLOBAL-05 (delete de la ACL completa). No-op si la ACL ya
        no existe (chequea existencia por nombre, no por tener reglas --
        una ACL con 0 reglas sigue existiendo como objeto en el device,
        ver "IP-Adm-V4-Int-ACL-global" real en f3r9s1)."""
        estado = self.reconciliar(device)
        actual = estado.get("actual")
        existe = actual is not None and actual.acls and any(a.get("name") == self.acl_delete for a in actual.acls)
        if not existe:
            return self._noop_resultado("borrar_acl")
        resultado = device.driver.delete_acl(self.acl_delete, device, device.password)
        return {**resultado, "accion": "borrar_acl"}

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
            "snmp_trap_hosts": self.snmp_trap_hosts,
            "ntp_servers": self.ntp_servers,
            "dns_servers": self.dns_servers,
            "log_servers": self.log_servers,
            "log_level": self.log_level,
            "routes": self.routes,
            "acls": self.acls,
            "arp_table": self.arp_table,
            "mac_table": self.mac_table,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GlobalConfig":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
