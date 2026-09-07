from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

PortMode = Literal["access", "trunk", "unknown"]

# Interface names on both Huawei and Cisco use letters, digits, slashes,
# colons, dots, dashes and underscores.  This regex is intentionally a
# whitelist rather than per-vendor format checks: rejecting outright weird
# input is enough at the API boundary; vendor-specific shape (Gi0/0/1 vs
# GigabitEthernet0/0/1) is the device's problem to refuse.
_INTERFACE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./:\-]{1,63}$")

# Cisco IOS description max is 200 chars; Huawei VRP max is 242 (S-series).
# 200 is the conservative shared ceiling.
_MAX_DESCRIPTION_LEN = 200

# Descriptions can contain spaces and the common ASCII punctuation operators
# actually type into them (and that survive both IOS and VRP).  Newlines and
# control characters are rejected to keep one command = one description.
_DESCRIPTION_INVALID_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_interface_name(interface: str) -> None:
    """Raise ``ValueError`` if *interface* is not a plausible interface id."""
    if not isinstance(interface, str) or not interface:
        raise ValueError("Interface name is required")
    if not _INTERFACE_NAME_RE.match(interface):
        raise ValueError(
            "Invalid interface name. Allowed characters are letters, digits, "
            "'.', '/', ':', '_', and '-'; name must start with a letter and "
            "be 2–64 characters long."
        )


def _validate_access_vlan_id(vlan_id: int) -> None:
    """Validate the VLAN ID for an access-port assignment.

    Rules:
        * must be a plain int (not a bool — Python treats bool as int)
        * must be in the standard switchport range 1–4094
        * must not be one of the IOS legacy FDDI/Token-Ring VLANs (1002–1005)
          which Cisco refuses to use as an access VLAN

    VLAN 1 is **allowed** because it is the platform default for an access
    port — operators who reset a port to default need to be able to express
    that. This differs from ``models.vlan._validate_vlan_not_reserved`` which
    is correct for *create/delete* operations on the VLAN itself.
    """
    if isinstance(vlan_id, bool) or not isinstance(vlan_id, int):
        raise ValueError("VLAN ID must be an integer")
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")
    if vlan_id in (1002, 1003, 1004, 1005):
        raise ValueError(
            f"VLAN {vlan_id} is reserved (Cisco IOS legacy FDDI/Token-Ring) "
            "and cannot be assigned as an access VLAN"
        )


def _validate_trunk_vlan_id(vlan_id: int) -> None:
    """Validate a single VLAN ID in a trunk allowed-VLAN list.

    Same range rules as access VLANs, but explicitly documented separately
    because trunk lists have different semantics (VLAN 1 appears commonly as
    a native VLAN that operators legitimately add or remove).
    """
    if isinstance(vlan_id, bool) or not isinstance(vlan_id, int):
        raise ValueError("VLAN ID must be an integer")
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")
    if vlan_id in (1002, 1003, 1004, 1005):
        raise ValueError(
            f"VLAN {vlan_id} is reserved (Cisco IOS legacy FDDI/Token-Ring) "
            "and cannot appear in a trunk allowed-VLAN list"
        )


def _validate_trunk_vlan_list(vlans: list) -> None:
    """Validate a list of VLAN IDs for trunk allowed-VLAN assignment.

    Rules:
        * must be a non-empty list
        * each item must pass ``_validate_trunk_vlan_id``
        * duplicates are silently accepted (the execution layer deduplicates)
    """
    if not isinstance(vlans, list):
        raise ValueError("vlans must be a list of integers")
    if len(vlans) == 0:
        raise ValueError("vlans must not be empty")
    for v in vlans:
        _validate_trunk_vlan_id(v)


def _validate_description(description: "str | None") -> None:
    """Raise ``ValueError`` if *description* is unsafe to push to a device.

    Empty descriptions are valid — they request a clear. The driver layer
    interprets the empty string as ``undo description`` / ``no description``.
    """
    if description is None:
        # Treat None as empty (clear). Callers that need strict typing
        # should enforce that at the Pydantic layer.
        return
    if not isinstance(description, str):
        raise ValueError("Description must be a string")
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"Description must not exceed {_MAX_DESCRIPTION_LEN} characters"
        )
    if _DESCRIPTION_INVALID_RE.search(description):
        raise ValueError("Description must not contain control characters or newlines")


@dataclass
class Puerto:
    """Domain representation of a switch interface — unifica lo que antes
    eran ``PortInfo`` (lado lectura) y ``PortConfigRequest`` (lado escritura,
    la clase real vieja se mantiene más abajo sin tocar, ver su docstring)
    en una sola. Implementa el contrato ``RecursoGestionable``
    (``validar``/``reconciliar``/``aplicar``/``repositorio`` — formalizado en
    Fase 5, ya satisfecho por forma desde esta fase).

    2 renames reales respecto al código viejo, resuelven un choque de
    nombres entre lectura y escritura: ``PortInfo.name`` +
    ``PortConfigRequest.interface`` → **``interface``** (gana escritura).
    ``PortInfo.admin_up`` + ``PortConfigRequest.admin_enabled`` →
    **``admin_up``** (gana lectura).

    Attributes
    ----------
    interface:
        Vendor-native interface identifier (requerido).
    device:
        Nombre del device al que pertenece — igual criterio que ``VLAN.device``.
    description, admin_up, mode, access_vlan, allowed_vlans, poe_enabled:
        Campos mutables — ``None`` significa "no aplica / no se está
        cambiando". Un valor no-``None`` en cualquiera de estos 6 es lo que
        ``mutation_fields`` detecta como intención de escritura.
    allowed_vlan_operation:
        Instrucción de la llamada ("add" reemplaza vs. suma al trunk
        existente) — no es un atributo persistente del puerto.
    operational_up, speed, duplex:
        Solo lectura, el device las reporta — ``aplicar()`` nunca las mira.

    Validation
    ----------
    ``__post_init__`` solo valida ``interface`` — seguro tanto para lectura
    (``reconciliar()`` construye un ``Puerto`` por cada entrada real que el
    device reporta) como para escritura. Las 3 reglas cruzadas que antes
    vivían en ``PortConfigRequest.__post_init__`` (mode requerido con
    access_vlan/allowed_vlans, access_vlan solo válido en access/trunk,
    allowed_vlans solo válido en trunk, al menos un campo de mutación
    presente) **no van acá** — un puerto real en modo ``"unknown"``/hybrid
    (confirmado en los parsers reales, Huawei) puede reportar ``access_vlan``
    seteado igual, y `__post_init__` corre también al leer. Esas 4 reglas
    viven en ``validar()``, que ``Orquestador`` solo llama antes de
    ``aplicar()`` — mismo criterio que ``VLAN.validar()`` (Fase 2, A1).
    """

    interface: str
    device: str = ""
    description: str | None = None
    admin_up: bool | None = None
    mode: PortMode | None = None
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None
    allowed_vlan_operation: str = "add"
    poe_enabled: bool | None = None
    storm_control_enabled: bool | None = None
    storm_control_threshold: float | None = None
    # RF-PUERTO-10 -- marcador de intención "reset a defaults", mismo
    # criterio que VLAN.eliminar: no es un campo de mutación más, es un
    # discriminador que aplicar()/validar() chequean primero y cortan ahí.
    reset: bool = False
    # -- solo lectura, el device las reporta, aplicar() nunca las mira --
    operational_up: bool | None = None
    speed: str | None = None
    duplex: str | None = None

    def __post_init__(self) -> None:
        _validate_interface_name(self.interface)

    @property
    def mutation_fields(self) -> set[str]:
        campos = ("description", "admin_up", "mode", "access_vlan",
                  "allowed_vlans", "poe_enabled", "storm_control_enabled",
                  "storm_control_threshold")
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        """Reglas de escritura — solo se llaman antes de ``aplicar()``, nunca
        durante ``reconciliar()``. Mismo criterio que ``VLAN.validar()``.

        Ya no hay una noción de "compuesto" genérico (``_es_composite`` se
        borró junto con el ``configure_port()`` de campo arbitrario que
        reemplazaba, ver ``aplicar()``) -- las 2 reglas cruzadas cuelgan
        directo de ``self.mode``: `mode="access"` exige `access_vlan`
        seteado, `mode="trunk"` exige `allowed_vlans` seteado. Cuando
        `mode` es `None` (los 4 endpoints de campo único -- `/access-vlan`,
        `/trunk-vlans` incluidos, que leen el modo en vivo del device en
        vez de recibirlo del caller) no hay regla cruzada que aplicar.

        Reusa ``self.mutation_fields`` en vez de mantener una 2da lista de
        campos acá -- bug real encontrado en una revisión de código: esta
        tupla tenía 5 campos, ``mutation_fields`` (usada por ``aplicar()``)
        tiene 6 (incluye ``poe_enabled``), así que un ``Puerto(poe_enabled=
        True)`` sin ningún otro campo pasaba por acá como "sin campo de
        mutación" a pesar de tener uno seteado. Hoy `poe_enabled` no es
        escribible por ningún endpoint real (solo aparece en `PortRead`,
        de solo lectura) así que era inalcanzable, pero las 2 listas podían
        desalinearse sin que nada lo notara -- ahora hay una sola.

        ``self.reset`` corta antes que todo lo demás -- mismo criterio que
        ``VLAN.validar()`` con ``self.eliminar``: un reset a defaults no
        necesita (ni debe validar) ningún otro campo, los ignora todos."""
        if self.reset:
            return
        if not self.mutation_fields:
            raise ValueError(
                "at least one mutation field must be provided "
                "(description, admin_up, mode, access_vlan, allowed_vlans, "
                "poe_enabled, storm_control_enabled, or reset)"
            )
        if self.storm_control_enabled is True and self.storm_control_threshold is None:
            raise ValueError("storm_control_enabled=True requires 'storm_control_threshold' to be set")
        if self.storm_control_threshold is not None and not (0 <= self.storm_control_threshold <= 100):
            raise ValueError("storm_control_threshold must be between 0 and 100")
        if self.mode == "access" and self.access_vlan is None:
            raise ValueError("mode='access' requires 'access_vlan' to be set")
        if self.mode == "trunk":
            # `access_vlan` es dual-purpose (ver docstring de la clase): VLAN
            # de acceso en modo access, PVID/native VLAN en modo trunk --
            # mismo campo, no uno nuevo. Un cambio de modo a trunk exige las
            # 2 dimensiones explícitas (PVID + lista permitida), sin
            # ambigüedad -- decisión real, no un default silencioso.
            if self.access_vlan is None:
                raise ValueError("mode='trunk' requires 'access_vlan' (native VLAN/PVID) to be set")
            if not self.allowed_vlans:
                raise ValueError("mode='trunk' requires 'allowed_vlans' to be set")
        if self.access_vlan is not None:
            _validate_access_vlan_id(self.access_vlan)
        if self.allowed_vlans is not None:
            _validate_trunk_vlan_list(self.allowed_vlans)
        if self.description is not None:
            _validate_description(self.description)

    def reconciliar(self, device: "Device") -> dict:
        puertos = device.driver.list_ports(device, device.password)
        existente = next((p for p in puertos if p.interface == self.interface), None)
        return {"existed": existente is not None, "actual": existente}

    @staticmethod
    def reconciliar_lote(recursos: "list[Puerto]", device: "Device") -> "list[dict]":
        """Lectura compartida para ``Orquestador.ejecutar_lote()`` -- 1 sola
        llamada a ``list_ports()`` (ya trae TODOS los puertos) en vez de 1
        por cada ``Puerto`` del lote. Devuelve 1 dict ``{"existed",
        "actual"}`` por entrada de *recursos*, EN EL MISMO ORDEN -- mismo
        shape por entrada que ``reconciliar()`` individual. Lista (no dict
        por interfaz) a propósito: alinea por posición con *recursos* sin
        necesitar una clave de identidad genérica del lado del
        orquestador (que no sabe si un recurso se identifica por
        interfaz, vlan_id, etc.)."""
        puertos = device.driver.list_ports(device, device.password)
        por_interfaz = {p.interface: p for p in puertos}
        return [
            {"existed": r.interface in por_interfaz, "actual": por_interfaz.get(r.interface)}
            for r in recursos
        ]

    def aplicar(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """Mismo criterio que VLAN.aplicar(): el dict devuelto siempre
        incluye "accion", agregado acá, no por el driver — Fase 3
        (AuditListener) lo necesita para RF-AUD-02.

        Dispatch por ``self.mode`` primero (dos operaciones con nombre,
        cada una con su propio método de driver -- ``set_access_mode()``/
        ``set_trunk_mode()``), después por el único campo restante para
        los 4 endpoints de campo único de siempre. Ya no hay un camino
        "compuesto" genérico que acepte cualquier combinación de campos
        (``_es_composite``/``configure_port(self, ...)`` -- retirados: la
        única combinación de 2+ campos que existía en la práctica era
        `mode` + su VLAN correspondiente, así que ahora tiene su propio
        método en vez de una rama genérica).

        *pre_state* -- igual criterio que VLAN.aplicar(): si viene seteado
        (``Orquestador.ejecutar()`` se lo pasa en el primer intento, con lo
        que ya capturó para su propio rollback) las ramas de campo único
        de abajo lo usan en vez de llamar su propio ``reconciliar()`` de
        nuevo. En ``None`` (reintentos reales, o default), cada rama relee
        el estado -- necesario ahí porque el device puede haber cambiado de
        verdad entre intentos. Las 2 ramas de modo no lo usan -- igual que
        el viejo camino compuesto, nunca llamaron ``reconciliar()`` acá.

        ``self.reset`` y storm-control se chequean antes que ``self.mode``
        -- ``reset`` es exclusivo con cualquier otro campo (RF-PUERTO-10,
        mismo nivel que ``VLAN.eliminar``). Storm-control es un par de
        campos (``storm_control_enabled`` + ``storm_control_threshold``),
        no encaja en el branch de "exactamente 1 campo" de abajo -- mismo
        motivo por el que ``mode`` tiene su propio branch en vez de contar
        como campo de mutación."""
        if self.reset:
            return self._aplicar_reset(device)
        if self.mode == "access":
            return self._aplicar_modo_access(device)
        if self.mode == "trunk":
            return self._aplicar_modo_trunk(device)
        campos = self.mutation_fields
        if "storm_control_enabled" in campos:
            return self._aplicar_storm_control(device, pre_state)
        if len(campos) != 1:
            raise ValueError(
                f"Puerto.aplicar(): sin mode seteado se espera exactamente "
                f"1 campo de mutación (se recibieron {sorted(campos)}) -- "
                f"las únicas combinaciones válidas de 2+ campos son "
                f"mode='access'+access_vlan, mode='trunk'+allowed_vlans, o "
                f"storm_control_enabled(+storm_control_threshold)"
            )
        campo = next(iter(campos))
        if campo == "description":
            return self._aplicar_description(device, pre_state)
        if campo == "admin_up":
            return self._aplicar_admin_up(device, pre_state)
        if campo == "access_vlan":
            return self._aplicar_access_vlan(device, pre_state)
        if campo == "allowed_vlans":
            return self._aplicar_allowed_vlans(device, pre_state)
        if campo == "poe_enabled":
            return self._aplicar_poe(device, pre_state)
        raise ValueError(f"Puerto.aplicar(): no hay driver call para el campo {campo!r}")

    def resolver_paso(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        """Ver ``RecursoGestionable.resolver_paso`` -- mismo dispatch que
        ``aplicar()`` (reset -> mode -> storm-control -> campo único),
        pero devuelve el paso sin tocar el device. Cada rama delega al
        ``_resolver_XXX`` correspondiente -- los mismos que usa
        ``aplicar()`` por debajo, no hay 2 caminos de no-op/dispatch."""
        if self.reset:
            return device.driver.resolver_reset_port(self.interface)
        if self.mode == "access":
            return device.driver.resolver_set_access_mode(self.interface, self.access_vlan)
        if self.mode == "trunk":
            return device.driver.resolver_set_trunk_mode(self.interface, self.access_vlan, list(self.allowed_vlans))
        campos = self.mutation_fields
        if "storm_control_enabled" in campos:
            return self._resolver_storm_control(device)
        if len(campos) != 1:
            raise ValueError(
                f"Puerto.resolver_paso(): sin mode seteado se espera exactamente "
                f"1 campo de mutación (se recibieron {sorted(campos)}) -- "
                f"las únicas combinaciones válidas de 2+ campos son "
                f"mode='access'+access_vlan, mode='trunk'+allowed_vlans, o "
                f"storm_control_enabled(+storm_control_threshold)"
            )
        campo = next(iter(campos))
        if campo == "description":
            return self._resolver_description(device, actual)
        if campo == "admin_up":
            return self._resolver_admin_up(device, actual)
        if campo == "access_vlan":
            return self._resolver_access_vlan(device, actual)
        if campo == "allowed_vlans":
            return self._resolver_allowed_vlans(device, actual)
        if campo == "poe_enabled":
            return self._resolver_poe(device, actual)
        raise ValueError(f"Puerto.resolver_paso(): no hay driver call para el campo {campo!r}")

    def _aplicar_modo_access(self, device: "Device") -> dict:
        """Cambia el puerto a modo access con ``self.access_vlan``,
        atómico -- reemplaza la rama ``_es_composite`` vieja para este caso
        puntual. Sin ``_noop_resultado()`` a propósito, mismo criterio que
        el camino que reemplaza: comparar "ya está en access con esta
        VLAN" es una pregunta legítima pero separada, documentada como
        alcance no resuelto, no un caso olvidado."""
        op_key, variant, vars = device.driver.resolver_set_access_mode(self.interface, self.access_vlan)
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_modo_access"}

    def _aplicar_modo_trunk(self, device: "Device") -> dict:
        """Cambia el puerto a modo trunk con ``self.access_vlan`` (PVID/
        native VLAN -- mismo campo dual-purpose que usa el modo access,
        ver docstring de la clase) y ``self.allowed_vlans``, atómico --
        mismo criterio que ``_aplicar_modo_access()``."""
        op_key, variant, vars = device.driver.resolver_set_trunk_mode(
            self.interface, self.access_vlan, list(self.allowed_vlans),
        )
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_modo_trunk"}

    def _noop_resultado(self, accion: str) -> dict:
        """Mismo shape que el no-op de VLAN.aplicar() -- corrección real de
        Fase 7 (RNF-API-05): las 4 ramas de campo único de aplicar() nunca
        comparaban contra el estado reconciliado antes de llamar al driver,
        a diferencia de VLAN.aplicar() (que sí tiene `noop: True` desde Fase
        2). Un PATCH repetido con el mismo valor volvía a mandar el comando
        al device cada vez."""
        return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": accion}

    def _resolver_description(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.description == self.description:
            return None
        return device.driver.resolver_update_port_description(self.interface, self.description)

    def _aplicar_description(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_description(device, actual)
        if paso is None:
            return self._noop_resultado("actualizar_descripcion_puerto")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "actualizar_descripcion_puerto"}

    def _resolver_admin_up(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.admin_up == self.admin_up:
            return None
        return device.driver.resolver_set_port_admin_state(self.interface, self.admin_up)

    def _aplicar_admin_up(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        accion = "activar_puerto" if self.admin_up else "desactivar_puerto"
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_admin_up(device, actual)
        if paso is None:
            return self._noop_resultado(accion)
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": accion}

    def _resolver_poe(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        if actual is not None and actual.poe_enabled == self.poe_enabled:
            return None
        return device.driver.resolver_set_port_poe(self.interface, self.poe_enabled)

    def _aplicar_poe(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-PUERTO-09. Mismo shape que ``_aplicar_admin_up()`` -- no-op si
        ya está en el estado pedido. La lectura de ``poe_enabled`` sigue
        siendo ``None`` en ambos parsers (no se agregó un 4to comando de
        lectura en esta pasada), así que ``actual.poe_enabled`` va a ser
        ``None`` casi siempre en la práctica -- el no-op solo dispara si el
        device en algún momento sí lo reporta."""
        accion = "activar_poe" if self.poe_enabled else "desactivar_poe"
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_poe(device, actual)
        if paso is None:
            return self._noop_resultado(accion)
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": accion}

    def _aplicar_access_vlan(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """`access_vlan` en un puerto trunk es el PVID, no el access VLAN --
        corrección real encontrada en Fase 5 (armando `Orquestador._rollback`
        contra `port_execution_service.py: _rollback_access_vlan()`, que sí
        distingue esto): despachar siempre a `set_port_access_vlan` es
        incorrecto sobre un puerto en modo trunk, donde el driver correcto
        es `set_trunk_pvid_vlan`. Necesita el modo actual del puerto -- lo
        trae *pre_state* (pasado por `Orquestador.ejecutar()` en el primer
        intento) o, si no vino, una lectura en vivo propia vía
        `reconciliar()`.

        El gate de modo ("access"/"trunk" solamente, rechaza "unknown") es
        otra corrección real encontrada comparando contra el `_validate()`
        real de `run_set_access_vlan_job()` -- sin esto, un puerto en modo
        no reconocido caía silenciosamente en la rama access."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_access_vlan(device, actual)
        if paso is None:
            # Corrección real de Fase 7 (RNF-API-05) -- ver _noop_resultado().
            return self._noop_resultado("asignar_vlan_acceso")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "asignar_vlan_acceso"}

    def _resolver_access_vlan(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        """Mismo gate/no-op/dispatch que ``_aplicar_access_vlan()`` de
        siempre (ver esa docstring), separado para que ``resolver_paso()``
        lo pueda usar sin tocar el device."""
        if actual is not None and actual.mode not in ("access", "trunk"):
            raise ValueError(
                f"el puerto {self.interface} no está en modo access ni trunk "
                f"(modo actual: {actual.mode!r}) — no se puede asignar VLAN de acceso"
            )
        if actual is not None and actual.access_vlan == self.access_vlan:
            return None
        if actual is not None and actual.mode == "trunk":
            return device.driver.resolver_set_trunk_pvid_vlan(self.interface, self.access_vlan)
        return device.driver.resolver_set_port_access_vlan(self.interface, self.access_vlan)

    def _aplicar_allowed_vlans(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """`allowed_vlan_operation` ("replace"/"add"/"remove") necesita la
        lista actual del trunk para calcular la lista final -- el driver
        siempre reemplaza completo (`FASE_1.md`: "el driver siempre
        realiza un reemplazo completo, no un delta"). Corrección real
        encontrada en Fase 5, comparando contra
        `port_execution_service.py: _compute_desired_vlans()`/
        `run_set_trunk_allowed_vlans_job()`: `aplicar()` ignoraba
        `allowed_vlan_operation` por completo y mandaba `self.allowed_vlans`
        tal cual al driver, sin importar "replace"/"add"/"remove" — y sin
        el guard real "remove no puede vaciar el trunk".

        El gate "el puerto debe estar en modo trunk" es otra corrección
        real encontrada comparando contra el mismo `_validate()` real --
        sin esto, `allowed_vlans` se podía "aplicar" sobre un puerto en
        modo access, mandando `set_trunk_allowed_vlans` a un puerto que
        nunca va a exponer esa config."""
        estado = pre_state if pre_state is not None else self.reconciliar(device)
        actual = estado.get("actual")
        paso = self._resolver_allowed_vlans(device, actual)
        if paso is None:
            # Corrección real de Fase 7 (RNF-API-05) -- ver _noop_resultado().
            return self._noop_resultado("configurar_trunk_vlans")
        op_key, variant, vars = paso
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": "configurar_trunk_vlans"}

    def _resolver_allowed_vlans(self, device: "Device", actual: "Puerto | None") -> "tuple[str, str | None, dict] | None":
        """Mismo gate/cómputo de delta/no-op que ``_aplicar_allowed_vlans()``
        de siempre (ver esa docstring), separado para que ``resolver_paso()``
        lo pueda usar sin tocar el device."""
        if actual is not None and actual.mode != "trunk":
            raise ValueError(
                f"el puerto {self.interface} no está en modo trunk "
                f"(modo actual: {actual.mode!r}) — no se puede modificar su lista "
                "de VLANs permitidas"
            )
        actuales = actual.allowed_vlans if actual is not None else None

        # Bug real encontrado en una revisión de código: `actuales is None`
        # (el puerto no se pudo leer -- timing, nombre de interfaz que el
        # parser no reconoce, etc.) caía en la misma rama que
        # `allowed_vlan_operation == "replace"`, sin importar si se había
        # pedido "add" o "remove" -- un remove sobre un estado no-leído
        # terminaba reemplazando el trunk entero por *solo* la lista que se
        # quería sacar, en vez de fallar loud. "replace" sigue siendo válido
        # sin estado previo (mismo criterio que _aplicar_modo_trunk, que
        # tampoco exige leer el estado antes de reemplazar); "add"/"remove"
        # ahora rechazan explícitamente cuando no hay lista actual con la
        # que calcular el delta.
        if self.allowed_vlan_operation == "replace":
            deseados = sorted(set(self.allowed_vlans))
        elif actuales is None:
            raise ValueError(
                f"no se pudo leer la lista de VLANs actual del puerto {self.interface} "
                f"-- no se puede calcular '{self.allowed_vlan_operation}' sin ese estado"
            )
        elif self.allowed_vlan_operation == "add":
            deseados = sorted(set(actuales) | set(self.allowed_vlans))
        elif self.allowed_vlan_operation == "remove":
            deseados = sorted(set(actuales) - set(self.allowed_vlans))
        else:
            raise ValueError(f"allowed_vlan_operation desconocido: {self.allowed_vlan_operation!r}")

        if not deseados:
            raise ValueError(
                f"la operación '{self.allowed_vlan_operation}' dejaría el puerto "
                f"{self.interface} sin VLANs permitidas en el trunk — rechazada"
            )

        if actuales is not None and sorted(set(actuales)) == deseados:
            # Comparación sobre la lista YA calculada (deseados), no sobre
            # self.allowed_vlans crudo -- "add"/"remove" son relativos al
            # estado actual, el no-op real es "¿el resultado final ya es
            # igual al estado actual?", no "¿la lista pedida es idéntica a
            # la actual?".
            return None

        return device.driver.resolver_set_trunk_allowed_vlans(self.interface, deseados)

    def _resolver_storm_control(self, device: "Device") -> "tuple[str, str | None, dict]":
        return device.driver.resolver_set_storm_control(
            self.interface, self.storm_control_enabled, self.storm_control_threshold,
        )

    def _aplicar_storm_control(self, device: "Device", pre_state: "dict | None" = None) -> dict:
        """RF-PUERTO-07, alcance simple (decisión con el usuario): un
        booleano + 1 threshold global (%), no los 3 tipos de tráfico
        (broadcast/multicast/unicast) por separado. Sin no-op -- igual que
        el modo, la lectura no expone storm-control hoy, así que no hay
        estado previo confiable contra el que comparar."""
        accion = "activar_storm_control" if self.storm_control_enabled else "desactivar_storm_control"
        op_key, variant, vars = self._resolver_storm_control(device)
        resultado = device.driver.aplicar_paso(op_key, variant, vars, device, device.password)
        return {**resultado, "accion": accion}

    def _aplicar_reset(self, device: "Device") -> dict:
        """RF-PUERTO-10, decisión con el usuario: "eliminar configuración
        del puerto" = reset a defaults (``default interface`` en Cisco,
        ``clear configuration interface`` en Huawei), no un borrado
        selectivo campo por campo. Sin pre_state/no-op a propósito -- mismo
        criterio que los 2 branches de modo: no vale la pena detectar "ya
        está en default" antes de mandar el comando."""
        resultado = device.driver.reset_port(self.interface, device, device.password)
        return {**resultado, "accion": "resetear_puerto"}

    def repositorio(self) -> str:
        return "puerto"

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict — todos los campos, formato interno
        (``interface``, no ``name``; la traducción a la forma de API vieja
        es responsabilidad explícita del router, ver FASE_5.md A7)."""
        return {
            "interface": self.interface,
            "device": self.device,
            "description": self.description,
            "admin_up": self.admin_up,
            "mode": self.mode,
            "access_vlan": self.access_vlan,
            "allowed_vlans": list(self.allowed_vlans) if self.allowed_vlans is not None else None,
            "allowed_vlan_operation": self.allowed_vlan_operation,
            "poe_enabled": self.poe_enabled,
            "storm_control_enabled": self.storm_control_enabled,
            "storm_control_threshold": self.storm_control_threshold,
            "reset": self.reset,
            "operational_up": self.operational_up,
            "speed": self.speed,
            "duplex": self.duplex,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Puerto":
        """Reconstruct a ``Puerto`` from a plain dict shaped like ``to_dict()``.
        Tolerant a keys faltantes (default None/""), solo ``interface`` es
        estrictamente requerido."""
        allowed = data.get("allowed_vlans")
        return cls(
            interface=data["interface"],
            device=data.get("device", ""),
            description=data.get("description"),
            admin_up=data.get("admin_up"),
            mode=data.get("mode"),
            access_vlan=data.get("access_vlan"),
            allowed_vlans=list(allowed) if allowed is not None else None,
            allowed_vlan_operation=data.get("allowed_vlan_operation", "add"),
            poe_enabled=data.get("poe_enabled"),
            storm_control_enabled=data.get("storm_control_enabled"),
            storm_control_threshold=data.get("storm_control_threshold"),
            reset=data.get("reset", False),
            operational_up=data.get("operational_up"),
            speed=data.get("speed"),
            duplex=data.get("duplex"),
        )

