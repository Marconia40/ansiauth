from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device

# SRS RF-INTERV-01: máximo 240 caracteres, whitelist alfanumérico + espacio/
# guión/punto -- a diferencia de Puerto.description (blacklist de
# caracteres de control nomás), acá el SRS pide una whitelist explícita.
_DESCRIPTION_VALID_RE = re.compile(r"^[A-Za-z0-9 .\-]*$")
_MAX_DESCRIPTION_LEN = 240

# Límite de servers de DHCP relay simultáneos por interfaz -- no verificado
# contra un modelo/vendor real todavía (SRS lo pide como excepción pero no
# da el número). Valor conservador de placeholder hasta confirmar el límite
# real de cada plataforma.
_MAX_DHCP_RELAY_SERVERS = 8


def _validate_vlan_id_range(vlan_id: int) -> None:
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")


def _validate_description(description: str) -> None:
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(f"Description must not exceed {_MAX_DESCRIPTION_LEN} characters")
    if not _DESCRIPTION_VALID_RE.match(description):
        raise ValueError(
            "Description contains invalid characters -- only letters, digits, "
            "spaces, hyphens and periods are allowed"
        )


@dataclass
class SVI:
    """Domain representation of a virtual interface (SVI) — ``interface
    Vlan{vlan_id}`` en Cisco, ``interface Vlanif{vlan_id}`` en Huawei.
    Implementa el contrato ``RecursoGestionable`` (``validar``/
    ``reconciliar``/``aplicar``/``repositorio``), mismo patrón que
    ``VLAN``/``Puerto``.

    Identidad
    ---------
    ``vlan_id`` es la identidad real de la interfaz, no un campo aparte --
    crear "Vlan10" ya es asociarla a VLAN 10 (RF-INTERV-9): no hay
    "reasignar a otra VLAN", eso es borrar y crear de nuevo, igual que en
    hardware real. ``validar()`` no chequea que la VLAN exista -- eso lo
    hace el endpoint de creación contra ``vlan_repository``/el estado real,
    antes de encolar (mismo criterio que otros chequeos de existencia en
    ``api/``, no es parte del contrato ``RecursoGestionable`` en sí).

    Campos mutables
    ----------------
    ``description``, ``admin_up``, ``ipv4_address``, ``ipv6_address``,
    ``acl_in``, ``acl_out``, ``dhcp_relay_servers``: ``None`` = no tocar.
    Para los de texto/lista, ``""``/``[]`` = limpiar (mismo convenio que
    ``Puerto.description``/``update_port_description``) -- un valor no-None
    en cualquiera de estos 7 es lo que ``mutation_fields`` detecta como
    intención de escritura.

    ``eliminar``: mismo patrón que ``VLAN.eliminar`` -- corta antes que
    todo lo demás en ``validar()``/``aplicar()``.
    """

    vlan_id: int
    device: str = ""
    description: str | None = None
    admin_up: bool | None = None
    ipv4_address: str | None = None
    ipv4_address_secondary: str | None = None
    ipv6_address: str | None = None
    acl_in: str | None = None
    acl_out: str | None = None
    dhcp_relay_add: str | None = None
    dhcp_relay_remove: str | None = None
    # -- solo lectura, poblado por reconciliar()/el parser -- aplicar()
    # nunca la toma como intención de escritura (RF-INTERV-05 es
    # incremental: agregar/eliminar 1 server a la vez, no full-replace).
    dhcp_relay_servers: list[str] | None = None
    eliminar: bool = False
    crear: bool = False
    # -- solo lectura, el device la reporta, aplicar() nunca la mira --
    operational_up: bool | None = None

    def __post_init__(self) -> None:
        _validate_vlan_id_range(self.vlan_id)

    @property
    def mutation_fields(self) -> set[str]:
        campos = ("description", "admin_up", "ipv4_address", "ipv4_address_secondary",
                  "ipv6_address", "acl_in", "acl_out", "dhcp_relay_add", "dhcp_relay_remove")
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        """Reglas de escritura -- solo se llaman antes de ``aplicar()``,
        nunca durante ``reconciliar()``. Mismo criterio que
        ``VLAN.validar()``/``Puerto.validar()``."""
        if self.eliminar:
            return
        if self.crear:
            if self.description is not None and self.description != "":
                _validate_description(self.description)
            return
        if self.dhcp_relay_add is not None and self.dhcp_relay_remove is not None:
            raise ValueError("cannot set dhcp_relay_add and dhcp_relay_remove in the same call")
        if not self.mutation_fields:
            raise ValueError(
                "at least one mutation field must be provided "
                "(description, admin_up, ipv4_address, ipv4_address_secondary, "
                "ipv6_address, acl_in, acl_out, dhcp_relay_add, dhcp_relay_remove, "
                "crear, or eliminar)"
            )
        if self.description is not None and self.description != "":
            _validate_description(self.description)

    def reconciliar(self, device: "Device") -> dict:
        """Estado actual de esta interfaz en *device*, leído en vivo --
        mismo criterio que ``VLAN.reconciliar()``/``Puerto.reconciliar()``:
        una escritura necesita el estado real del momento, no la cache que
        sirve ``GET /svis``."""
        interfaces = device.driver.get_svis(device, device.password)
        existente = next((i for i in interfaces if i.vlan_id == self.vlan_id), None)
        return {"existed": existente is not None, "actual": existente}

    @staticmethod
    def reconciliar_lote(recursos: "list[SVI]", device: "Device") -> "list[dict]":
        """Ver ``Puerto.reconciliar_lote()`` -- mismo criterio, 1 sola
        llamada a ``get_svis()`` en vez de 1 por cada ``SVI`` del lote."""
        interfaces = device.driver.get_svis(device, device.password)
        por_vlan = {i.vlan_id: i for i in interfaces}
        return [
            {"existed": r.vlan_id in por_vlan, "actual": por_vlan.get(r.vlan_id)}
            for r in recursos
        ]

    @staticmethod
    def ajustar_estados_lote(recursos: "list[SVI]", estados: "list[dict]") -> "list[dict]":
        """Bug real encontrado probando un batch {ipv4_address, dhcp_relay_add}
        junto: ``_resolver_dhcp_relay_add``/``_resolver_ipv4_secondary``
        validan su precondition ("¿la interfaz tiene IPv4/IPv6?") contra
        ``actual`` -- el estado leído ANTES de aplicar nada del lote. Si el
        mismo lote está fijando esa IPv4/IPv6 por primera vez, la
        validación fallaba con un falso "interface has no IPv4 address
        configured" pese a que el comando de IPv4 se manda ANTES en la
        MISMA conexión (mismo orden que ``expandir_a_svis()`` ya arma:
        ipv4_address antes que ipv4_address_secondary/dhcp_relay_*) -- en
        el device de verdad funcionaría. Este método corre antes de
        ``resolver_paso()`` (llamado por ``Orquestador.ejecutar_lote()``
        vía ``getattr(tipo, "ajustar_estados_lote", None)``, opcional --
        ``Puerto`` no lo necesita, ningún campo suyo depende de otro campo
        del MISMO lote) y le pega al ``actual`` de cada entrada el
        ipv4_address/ipv6_address "efectivo" -- el que otro recurso del
        MISMO vlan_id esté por fijar en este lote, si lo hay, si no el
        leído. No toca ningún otro campo (ni siquiera
        ipv4_address_secondary/dhcp_relay_servers) -- el no-op check de
        esos sigue comparando contra el valor real leído.

        Bug real encontrado probando ESTE fix: si se incluye al propio
        recurso al armar "qué está fijando el lote", el resolver de
        ``ipv4_address``/``ipv6_address`` termina comparando su ``actual``
        parchado contra SU PROPIO valor nuevo -- siempre "igual", siempre
        no-op, ni un solo `ipv4_address` se llegaba a aplicar más. El
        parche para la entrada ``i`` tiene que salir de los OTROS
        recursos del lote (``j != i``), nunca del propio."""
        ajustados: "list[dict]" = []
        for i, (r, estado) in enumerate(zip(recursos, estados)):
            ipv4_nuevo = None
            ipv6_nuevo = None
            for j, otro in enumerate(recursos):
                if j == i or otro.vlan_id != r.vlan_id:
                    continue
                if otro.ipv4_address:
                    ipv4_nuevo = otro.ipv4_address
                if otro.ipv6_address:
                    ipv6_nuevo = otro.ipv6_address
            if ipv4_nuevo is None and ipv6_nuevo is None:
                ajustados.append(estado)
                continue
            actual = estado.get("actual")
            base = actual if actual is not None else SVI(vlan_id=r.vlan_id)
            actual_efectivo = replace(
                base,
                ipv4_address=ipv4_nuevo if ipv4_nuevo is not None else base.ipv4_address,
                ipv6_address=ipv6_nuevo if ipv6_nuevo is not None else base.ipv6_address,
            )
            ajustados.append({**estado, "actual": actual_efectivo})
        return ajustados

    def resolver_paso(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        """Ver ``RecursoGestionable.resolver_paso``. ``dhcp_relay_add``/
        ``dhcp_relay_remove`` SÍ batchean -- el front (``SVIEditModal``)
        limita a 1 solo cambio de DHCP relay encolado por Save, así que
        acá nunca hay más de 1 delta por lote a plegar contra la misma
        lectura de ``actual`` (ver docstring de ``_resolver_dhcp_relay_add``/
        ``_resolver_dhcp_relay_remove``). ``crear``/``eliminar`` quedan
        afuera -- son operaciones de ciclo de vida, no "cambios de campo"
        batcheables junto con el resto."""
        campos = self.mutation_fields
        if len(campos) != 1:
            raise ValueError(
                f"SVI.resolver_paso(): se espera exactamente 1 campo de "
                f"mutación por llamada (se recibieron {sorted(campos)})"
            )
        campo = next(iter(campos))
        if campo == "description":
            return self._resolver_description(device, actual)
        if campo == "admin_up":
            return self._resolver_admin_up(device, actual)
        if campo == "ipv4_address":
            return self._resolver_ipv4(device, actual)
        if campo == "ipv4_address_secondary":
            return self._resolver_ipv4_secondary(device, actual)
        if campo == "ipv6_address":
            return self._resolver_ipv6(device, actual)
        if campo in ("acl_in", "acl_out"):
            return self._resolver_acl(device, actual, campo)
        if campo == "dhcp_relay_add":
            return self._resolver_dhcp_relay_add(device, actual)
        if campo == "dhcp_relay_remove":
            return self._resolver_dhcp_relay_remove(device, actual)
        raise ValueError(f"SVI.resolver_paso(): campo no batcheable {campo!r}")

    def aplicar(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """Mismo criterio que ``VLAN.aplicar()``/``Puerto.aplicar()``: el
        dict devuelto siempre incluye "accion", agregado acá, no por el
        driver."""
        if self.eliminar:
            estado = pre_state if pre_state is not None else self.reconciliar(device)
            if not estado["existed"]:
                return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": "eliminar_svi"}
            resultado = device.driver.delete_svi(self.vlan_id, device, device.password)
            return {**resultado, "accion": "eliminar_svi"}

        if self.crear:
            return self._aplicar_crear(device, pre_state)

        campos = self.mutation_fields
        if len(campos) != 1:
            raise ValueError(
                f"SVI.aplicar(): se espera exactamente 1 campo de "
                f"mutación por llamada (se recibieron {sorted(campos)}) -- "
                f"un endpoint por campo, mismo criterio que Puerto"
            )
        campo = next(iter(campos))
        if campo == "description":
            return self._aplicar_description(device, pre_state)
        if campo == "admin_up":
            return self._aplicar_admin_up(device, pre_state)
        if campo == "ipv4_address":
            return self._aplicar_ipv4(device, pre_state)
        if campo == "ipv4_address_secondary":
            return self._aplicar_ipv4_secondary(device, pre_state)
        if campo == "ipv6_address":
            return self._aplicar_ipv6(device, pre_state)
        if campo in ("acl_in", "acl_out"):
            return self._aplicar_acl(device, pre_state, campo)
        if campo == "dhcp_relay_add":
            return self._aplicar_dhcp_relay_add(device, pre_state)
        if campo == "dhcp_relay_remove":
            return self._aplicar_dhcp_relay_remove(device, pre_state)
        raise ValueError(f"SVI.aplicar(): no hay driver call para el campo {campo!r}")

    def _noop_resultado(self, accion: str, **extra: object) -> dict:
        return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": accion, **extra}

    def _aplicar_crear(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-INTERV-01. Curso alternativo del SRS: "Interfaz ya existente:
        el sistema notifica duplicado y no crea la nueva" -- reconciliar()
        primero, en vez de asumir que re-aplicar el comando de creación es
        inofensivo (lo es en la CLI, pero no es lo que pide el spec:
        devolver algo distinguible como "duplicate", no reintentar en
        silencio). Si el parámetro description vino (RF-INTERV-01 lo
        acepta opcional al crear), se aplica atómicamente después."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        if estado["existed"]:
            return self._noop_resultado("crear_svi", duplicate=True)
        resultado = device.driver.create_svi(self.vlan_id, device, device.password)
        if not resultado.get("success") or not self.description:
            return {**resultado, "accion": "crear_svi"}
        desc_resultado = device.driver.set_svi_description(
            self.vlan_id, self.description, device, device.password,
        )
        return {**desc_resultado, "accion": "crear_svi"}

    def _resolver_description(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.description == self.description:
            return None
        return device.driver.resolver_set_svi_description(self.vlan_id, self.description)

    def _aplicar_description(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_description(device, actual)
        if paso is None:
            return self._noop_resultado("actualizar_descripcion_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "actualizar_descripcion_svi"}

    def _resolver_admin_up(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.admin_up == self.admin_up:
            return None
        return device.driver.resolver_set_svi_admin_state(self.vlan_id, self.admin_up)

    def _aplicar_admin_up(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        accion = "activar_svi" if self.admin_up else "desactivar_svi"
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_admin_up(device, actual)
        if paso is None:
            return self._noop_resultado(accion)
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": accion}

    def _resolver_ipv4(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.ipv4_address == self.ipv4_address:
            return None
        return device.driver.resolver_set_svi_ipv4(self.vlan_id, self.ipv4_address)

    def _aplicar_ipv4(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_ipv4(device, actual)
        if paso is None:
            return self._noop_resultado("configurar_ipv4_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_ipv4_svi"}

    def _resolver_ipv4_secondary(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        """RF-INTERV-03: "IP secundaria sin IP primaria" -- chequeo de
        estado real, mismo criterio que ``_aplicar_ipv4_secondary()`` de
        siempre (ver esa docstring)."""
        if self.ipv4_address_secondary and not (actual and actual.ipv4_address):
            raise ValueError(
                "cannot set a secondary IPv4 address: interface has no primary IPv4 address configured"
            )
        if actual is not None and actual.ipv4_address_secondary == self.ipv4_address_secondary:
            return None
        previa = actual.ipv4_address_secondary if actual is not None else None
        return device.driver.resolver_set_svi_ipv4_secondary(self.vlan_id, self.ipv4_address_secondary, previa)

    def _aplicar_ipv4_secondary(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-INTERV-03: "IP secundaria sin IP primaria: se intenta asignar
        una IP secundaria sin que exista una primaria en la interfaz" --
        chequeo de estado real (reconciliar()), no de payload: una request
        que también trae ipv4_address en el mismo llamado ya es rechazada
        más arriba (aplicar() exige exactamente 1 campo de mutación por
        llamada), así que "primaria" acá solo puede venir de lo que el
        device ya tiene configurado."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_ipv4_secondary(device, actual)
        if paso is None:
            return self._noop_resultado("configurar_ipv4_secundaria_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_ipv4_secundaria_svi"}

    def _resolver_ipv6(self, device: "Device", actual: "SVI | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.ipv6_address == self.ipv6_address:
            return None
        return device.driver.resolver_set_svi_ipv6(self.vlan_id, self.ipv6_address)

    def _aplicar_ipv6(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_ipv6(device, actual)
        if paso is None:
            return self._noop_resultado("configurar_ipv6_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_ipv6_svi"}

    def _resolver_acl(
        self, device: "Device", actual: "SVI | None", campo: str = "acl_in",
    ) -> "tuple[str, str | None, dict] | None":
        direccion = "in" if campo == "acl_in" else "out"
        valor = self.acl_in if campo == "acl_in" else self.acl_out
        actual_valor = getattr(actual, campo) if actual is not None else None
        # "" (clear) contra None (nada atado en el device) es el mismo
        # estado -- sin esta normalización, un clear pedido sobre una
        # dirección que ya no tiene nada atado no se detectaba como no-op
        # (None != "") y llegaba al driver sin ACL de referencia para el
        # "undo".
        if actual is not None and (actual_valor or None) == (valor or None):
            return None
        return device.driver.resolver_set_svi_acl(self.vlan_id, direccion, valor, current_acl_name=actual_valor)

    def _aplicar_acl(self, device: "Device", pre_state: "dict | None" = None, campo: str = "acl_in") -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_acl(device, actual, campo)
        if paso is None:
            return self._noop_resultado("configurar_acl_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_acl_svi"}

    @staticmethod
    def _es_ipv6(ip: str) -> bool:
        return ":" in ip

    def _resolver_dhcp_relay_add(
        self, device: "Device", actual: "SVI | None",
    ) -> "tuple[str, str | None, dict] | None":
        """RF-INTERV-05: incremental (1 server por llamada), no full-replace
        -- se conoce la lista actual vía ``actual`` (leída 1 vez, sea por
        ``reconciliar()`` individual o por ``reconciliar_lote()`` cuando
        viene de un batch), se agrega 1 IP, y se manda la lista completa
        al driver (que sí sigue siendo full-replace puertas adentro,
        mismo mecanismo ya construido). El SRS pide 2 preconditions de
        estado real: la IP ya está (duplicado, noop) y la interfaz debe
        tener IPv4/IPv6 configurada según la familia del server que se
        agrega. Batchea sin plegado especial porque el front
        (``SVIEditModal``) limita a 1 solo cambio de DHCP relay encolado
        por Save -- nunca hay un 2do delta contra el mismo ``actual`` que
        pueda pisar a este."""
        servers_actuales = list(actual.dhcp_relay_servers) if actual and actual.dhcp_relay_servers else []
        ip = self.dhcp_relay_add
        if self._es_ipv6(ip):
            if not (actual and actual.ipv6_address):
                raise ValueError(
                    "cannot add an IPv6 DHCP relay server: interface has no IPv6 address configured"
                )
        elif not (actual and actual.ipv4_address):
            raise ValueError(
                "cannot add an IPv4 DHCP relay server: interface has no IPv4 address configured"
            )
        if ip in servers_actuales:
            return None
        if len(servers_actuales) >= _MAX_DHCP_RELAY_SERVERS:
            raise ValueError(f"cannot add DHCP relay server: limit of {_MAX_DHCP_RELAY_SERVERS} reached")
        return device.driver.resolver_set_svi_dhcp_relay(self.vlan_id, servers_actuales + [ip])

    def _aplicar_dhcp_relay_add(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_dhcp_relay_add(device, actual)
        if paso is None:
            return self._noop_resultado("agregar_dhcp_relay_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "agregar_dhcp_relay_svi"}

    def _resolver_dhcp_relay_remove(
        self, device: "Device", actual: "SVI | None",
    ) -> "tuple[str, str | None, dict] | None":
        """El SRS pide "sin error crítico" si la IP a eliminar no estaba --
        mismo criterio de noop que el resto del contrato (VLAN/Puerto)."""
        servers_actuales = list(actual.dhcp_relay_servers) if actual and actual.dhcp_relay_servers else []
        ip = self.dhcp_relay_remove
        if ip not in servers_actuales:
            return None
        return device.driver.resolver_set_svi_dhcp_relay(
            self.vlan_id, [s for s in servers_actuales if s != ip],
        )

    def _aplicar_dhcp_relay_remove(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_dhcp_relay_remove(device, actual)
        if paso is None:
            return self._noop_resultado("eliminar_dhcp_relay_svi")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "eliminar_dhcp_relay_svi"}

    def repositorio(self) -> str:
        return "svi"

    def resumen_intento(self) -> str:
        """Ver ``VLAN.resumen_intento()`` -- misma idea, adaptada a que acá
        ``eliminar``/``crear`` son flags aparte de ``mutation_fields`` (no
        entran en ese set, ver ``validar()``)."""
        identidad = f"SVI {self.vlan_id} on {self.device}" if self.device else f"SVI {self.vlan_id}"
        if self.eliminar:
            return f"Delete {identidad}"
        if self.crear:
            return f"Create {identidad}"
        campos = self.mutation_fields
        if not campos:
            return f"{identidad}: no changes"
        cambios = ", ".join(f"set {campo} to {getattr(self, campo)!r}" for campo in sorted(campos))
        return f"{identidad}: {cambios}"

    def to_dict(self) -> dict:
        return {
            "vlan_id": self.vlan_id,
            "device": self.device,
            "description": self.description,
            "admin_up": self.admin_up,
            "ipv4_address": self.ipv4_address,
            "ipv4_address_secondary": self.ipv4_address_secondary,
            "ipv6_address": self.ipv6_address,
            "acl_in": self.acl_in,
            "acl_out": self.acl_out,
            "dhcp_relay_servers": list(self.dhcp_relay_servers) if self.dhcp_relay_servers is not None else None,
            "operational_up": self.operational_up,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SVI":
        servers = data.get("dhcp_relay_servers")
        return cls(
            vlan_id=data["vlan_id"],
            device=data.get("device", ""),
            description=data.get("description"),
            admin_up=data.get("admin_up"),
            ipv4_address=data.get("ipv4_address"),
            ipv4_address_secondary=data.get("ipv4_address_secondary"),
            ipv6_address=data.get("ipv6_address"),
            acl_in=data.get("acl_in"),
            acl_out=data.get("acl_out"),
            dhcp_relay_servers=list(servers) if servers is not None else None,
            operational_up=data.get("operational_up"),
        )
