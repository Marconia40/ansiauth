from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

PortMode = Literal["access", "trunk", "unknown"]
PortConfigMode = Literal["access", "trunk"]

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
    # -- solo lectura, el device las reporta, aplicar() nunca las mira --
    operational_up: bool | None = None
    speed: str | None = None
    duplex: str | None = None

    def __post_init__(self) -> None:
        _validate_interface_name(self.interface)

    @property
    def mutation_fields(self) -> set[str]:
        campos = ("description", "admin_up", "mode", "access_vlan",
                  "allowed_vlans", "poe_enabled")
        return {c for c in campos if getattr(self, c) is not None}

    def validar(self) -> None:
        """Reglas de escritura — solo se llaman antes de ``aplicar()``, nunca
        durante ``reconciliar()``. Mismo criterio que ``VLAN.validar()``."""
        _mutation_fields = (
            self.description, self.admin_up, self.mode,
            self.access_vlan, self.allowed_vlans,
        )
        if all(v is None for v in _mutation_fields):
            raise ValueError(
                "at least one mutation field must be provided "
                "(description, admin_up, mode, access_vlan, or allowed_vlans)"
            )
        if self.access_vlan is not None and self.mode not in ("access", "trunk"):
            raise ValueError(
                f"'access_vlan' (PVID) may only be set when mode='access' or mode='trunk' "
                f"(got mode={self.mode!r})"
            )
        if self.allowed_vlans is not None and self.mode != "trunk":
            raise ValueError(
                f"'allowed_vlans' may only be set when mode='trunk' "
                f"(got mode={self.mode!r})"
            )
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

    def aplicar(self, device: "Device") -> dict:
        """Mismo criterio que VLAN.aplicar(): el dict devuelto siempre
        incluye "accion", agregado acá, no por el driver — Fase 3
        (AuditListener) lo necesita para RF-AUD-02. `configure_port()`
        devuelve un `PortConfigResult` (dataclass), no un dict como los
        métodos puntuales — se normaliza acá con `.to_dict()` para que el
        caller de `aplicar()` siempre reciba la misma forma (dict), sin
        importar cuántos campos cambiaron.

        `"rc"` se agrega también acá — corrección real encontrada en Fase 5
        armando `Orquestador`: `PortConfigResult.to_dict()` no tiene clave
        `"rc"` (usa `success`/`changed`), pero `Orquestador.ejecutar()`
        chequea `resultado.get("rc", 0) != 0` de forma uniforme para
        cualquier `RecursoGestionable` — sin esta clave, una falla real de
        `configure_port()` quedaba invisible para `Orquestador` (default
        `0` = "éxito"), confirmado con un test end-to-end (`configure_port`
        devolviendo `success=False` no disparaba ni excepción ni rollback)."""
        campos = self.mutation_fields
        if len(campos) > 1:
            resultado = device.driver.configure_port(self, device, device.password)
            return {
                **resultado.to_dict(),
                "rc": 0 if resultado.success else 1,
                "accion": "configurar_puerto",
            }
        campo = next(iter(campos))
        if campo == "description":
            resultado = device.driver.update_port_description(self.interface, self.description, device, device.password)
            return {**resultado, "accion": "actualizar_descripcion_puerto"}
        if campo == "admin_up":
            resultado = device.driver.set_port_admin_state(self.interface, self.admin_up, device, device.password)
            return {**resultado, "accion": "activar_puerto" if self.admin_up else "desactivar_puerto"}
        if campo == "access_vlan":
            resultado = device.driver.set_port_access_vlan(self.interface, self.access_vlan, device, device.password)
            return {**resultado, "accion": "asignar_vlan_acceso"}
        if campo == "allowed_vlans":
            resultado = device.driver.set_trunk_allowed_vlans(self.interface, self.allowed_vlans, device, device.password)
            return {**resultado, "accion": "configurar_trunk_vlans"}
        raise ValueError(f"Puerto.aplicar(): no hay driver call para el campo {campo!r}")

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
            operational_up=data.get("operational_up"),
            speed=data.get("speed"),
            duplex=data.get("duplex"),
        )


@dataclass
class PortConfigRequest:
    """Domain model for a composite port write operation.

    **No absorbida en ``Puerto`` — corrección real encontrada implementando
    Fase 2, A2.** El plan original decía que ``Puerto`` reemplaza esta clase
    y que se borra en esta fase. Falso: ``port_config_service.py`` la importa
    a nivel de módulo (línea 29), y ``api/ports.py`` importa
    ``port_config_service`` a nivel de módulo también — borrarla acá rompe
    ``import app.api.ports`` (y por lo tanto ``app.main``), mismo tipo de
    problema que ``validators/vlan_validator.py``/``validators/port_validator.py``
    (ver nota ahí). Se mantiene **sin ningún cambio** respecto al código
    original hasta que Fase 5 rewiree ``api/ports.py``/borre
    ``port_config_service.py`` — agregada a la tabla de limpieza de
    `FASE_7.md`.

    Step 3.1 introduces this model as the architectural foundation for
    multi-field port configuration.  Each field is optional — only the
    supplied fields will be applied.  Validation in ``__post_init__``
    catches cross-field constraint violations before any network I/O.

    Attributes
    ----------
    device:
        Target device name (must be registered in the device inventory).
    interface:
        Vendor-native interface identifier (required; cannot be empty).
    description:
        New description text.  ``""`` clears the description; ``None`` means
        "do not change the description".
    admin_enabled:
        ``True`` → bring the port up; ``False`` → shut it down;
        ``None`` → do not change admin state.
    mode:
        Switchport mode to configure.  ``None`` means "do not change mode".
        Required when ``access_vlan`` or ``allowed_vlans`` is also set,
        because those fields only make sense in a specific mode.
    access_vlan:
        Access VLAN to assign.  Only valid when ``mode='access'``.
    allowed_vlans:
        Trunk allowed-VLAN list.  Only valid when ``mode='trunk'``.

    Validation rules
    ----------------
    * ``interface`` must be a non-empty string.
    * At least one mutation field (description, admin_enabled, mode,
      access_vlan, allowed_vlans) must be non-``None``.
    * ``access_vlan`` is only valid when ``mode='access'``.
    * ``allowed_vlans`` is only valid when ``mode='trunk'``.
    """

    device: str
    interface: str
    description: str | None = None
    admin_enabled: bool | None = None
    mode: PortConfigMode | None = None
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None
    allowed_vlan_operation: str = "add"

    def __post_init__(self) -> None:
        if not self.interface:
            raise ValueError("'interface' is required and must be a non-empty string")

        _mutation_fields = (
            self.description,
            self.admin_enabled,
            self.mode,
            self.access_vlan,
            self.allowed_vlans,
        )
        if all(v is None for v in _mutation_fields):
            raise ValueError(
                "at least one mutation field must be provided "
                "(description, admin_enabled, mode, access_vlan, or allowed_vlans)"
            )

        if self.access_vlan is not None and self.mode not in ("access", "trunk"):
            raise ValueError(
                f"'access_vlan' (PVID) may only be set when mode='access' or mode='trunk' "
                f"(got mode={self.mode!r})"
            )

        if self.allowed_vlans is not None and self.mode != "trunk":
            raise ValueError(
                f"'allowed_vlans' may only be set when mode='trunk' "
                f"(got mode={self.mode!r})"
            )

    @property
    def has_vlan_change(self) -> bool:
        """True if this request includes any VLAN-related field."""
        return self.access_vlan is not None or self.allowed_vlans is not None

    @property
    def mutation_fields(self) -> list[str]:
        """Names of the mutation fields that are non-None in this request."""
        fields = []
        if self.description is not None:
            fields.append("description")
        if self.admin_enabled is not None:
            fields.append("admin_enabled")
        if self.mode is not None:
            fields.append("mode")
        if self.access_vlan is not None:
            fields.append("access_vlan")
        if self.allowed_vlans is not None:
            fields.append("allowed_vlans")
        return fields


@dataclass
class PortInfo:
    """Normalized domain representation of a single switch interface.

    **No absorbida en ``Puerto`` — mismo motivo que ``PortConfigRequest``
    arriba, corrección real encontrada implementando Fase 2, A2.**
    ``port_service.py`` la importa a nivel de módulo (``from
    app.models.port import PortInfo, PortListResponse``), y ``api/ports.py``
    importa ``port_service`` a nivel de módulo — borrarla acá rompe
    ``import app.api.ports``/``app.main`` igual que ``PortConfigRequest``.
    Se mantiene sin cambios hasta que Fase 5 borre ``port_service.py`` —
    agregada a la tabla de limpieza de `FASE_7.md` junto con las otras 3.

    Vendor-neutral by design: every concrete port driver (Cisco, Huawei, ...)
    parses its own CLI output and returns ``Puerto`` objects now (Fase 2) —
    this class survives only as ``port_service.py``'s own dead-until-Fase-5
    representation, not used by any vendor driver anymore.

    Attributes
    ----------
    name:
        Canonical interface identifier as reported by the device
        (e.g. ``"GigabitEthernet0/0/1"``).
    description:
        Operator-supplied description string, or ``None`` if the device does
        not report one for this interface.  Empty strings are normalized
        to ``None``.
    admin_up:
        Administrative state — ``True`` when the port is *not* shut down.
        Maps to ``"no shutdown"`` on Cisco or absence of ``"shutdown"`` on
        Huawei VRP.  ``None`` only when the platform does not expose this.
    operational_up:
        Line-protocol state — ``True`` when the port is currently passing
        traffic at L1/L2.  Distinct from ``admin_up``: a port can be
        administratively up but operationally down (no link).
    mode:
        Switchport mode: ``"access"``, ``"trunk"``, or ``"unknown"`` when the
        platform reports a value that does not map to either (e.g. ``hybrid``
        on Huawei VRP).
    access_vlan:
        VLAN ID assigned to an access port, or the native VLAN of a trunk
        port (Huawei reports this uniformly as PVID for both modes).
        ``None`` when unknown.
    allowed_vlans:
        For trunk ports, the list of VLAN IDs that the trunk is permitted to
        carry.  ``None`` for access ports or when the platform does not
        expose the trunk VLAN list.  Always sorted ascending.
    poe_enabled:
        Power-over-Ethernet enable state.  ``None`` when the device is not
        PoE-capable or the platform does not report this field.
    speed:
        Negotiated link speed string (e.g. ``"1000Mbps"``), or ``None`` when
        the platform does not expose it in the chosen read commands.
    duplex:
        Negotiated duplex string (e.g. ``"FULL"``, ``"HALF"``), or ``None``.

    Notes
    -----
    The contract is "missing information becomes ``None``".  Parsers must
    never invent values to fill gaps — a missing field is communicated by
    leaving the attribute at its ``None`` default.
    """

    name: str
    description: str | None = None
    admin_up: bool | None = None
    operational_up: bool | None = None
    mode: PortMode = "unknown"
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None
    poe_enabled: bool | None = None
    speed: str | None = None
    duplex: str | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses and persistence.

        All keys are always present so consumers can rely on a stable shape;
        unknown values are emitted as ``null``.

        Returns
        -------
        dict
            Wire-format representation with the same field names as the
            domain object.
        """
        return {
            "name": self.name,
            "description": self.description,
            "admin_up": self.admin_up,
            "operational_up": self.operational_up,
            "mode": self.mode,
            "access_vlan": self.access_vlan,
            "allowed_vlans": list(self.allowed_vlans) if self.allowed_vlans is not None else None,
            "poe_enabled": self.poe_enabled,
            "speed": self.speed,
            "duplex": self.duplex,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PortInfo":
        """Reconstruct a ``PortInfo`` from a plain dict.

        Tolerant to missing keys (which default to ``None``) so callers can
        rehydrate partial snapshots — e.g. a record produced by an older
        parser version that lacked PoE awareness.

        Parameters
        ----------
        data:
            Dict shaped like the output of ``to_dict()``.  Only ``name`` is
            strictly required.

        Returns
        -------
        PortInfo
        """
        allowed = data.get("allowed_vlans")
        return cls(
            name=data["name"],
            description=data.get("description"),
            admin_up=data.get("admin_up"),
            operational_up=data.get("operational_up"),
            mode=data.get("mode", "unknown"),
            access_vlan=data.get("access_vlan"),
            allowed_vlans=list(allowed) if allowed is not None else None,
            poe_enabled=data.get("poe_enabled"),
            speed=data.get("speed"),
            duplex=data.get("duplex"),
        )


@dataclass
class PortListResponse:
    """Envelope returned by ``VendorDriver.list_ports`` and ``port_service.list_ports``.

    Carries the normalized port list plus a small piece of provenance
    metadata so callers can distinguish "device returned zero interfaces" from
    "device returned interfaces we could not parse". Mirrors the orchestration
    style already used by the VLAN layer where the contract is a plain list,
    but here we wrap the list because port responses tend to be larger and
    benefit from a stable envelope.

    Attributes
    ----------
    device:
        Device name the response refers to. Allows callers to confirm that
        a fan-out (group execution) returned data for the expected device.
    ports:
        Normalized port entries. Always present (possibly empty).
    vendor:
        Vendor identifier string the driver belongs to (e.g. ``"huawei_vrp"``).
        Useful for downstream consumers that render vendor-specific hints.
    """

    device: str
    ports: list[Puerto] = field(default_factory=list)
    vendor: str | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses."""
        return {
            "device": self.device,
            "vendor": self.vendor,
            "ports": [p.to_dict() for p in self.ports],
            "count": len(self.ports),
        }


@dataclass
class PortConfigResult:
    """Domain model for the result of a composite port write operation.

    Returned by ``VendorDriver.configure_port`` implementations. Fields
    marked optional (default ``None``) are populated by the orchestration
    layer or the driver when the information is available.

    Attributes
    ----------
    success:
        ``True`` iff all requested mutations were applied without error.
    changed:
        ``True`` iff at least one field on the device was actually changed
        (i.e. the operation was not a complete no-op).
    interface:
        Vendor-native interface identifier the operation targeted.
    vendor:
        Vendor driver string (e.g. ``"huawei_vrp"``). ``None`` when the
        driver does not report it.
    execution_time_ms:
        Wall-clock time the driver call consumed, in milliseconds.
    rollback_performed:
        ``True`` if a rollback was triggered after a failure. ``None``
        when not applicable (success path, or rollback not supported).
    warnings:
        Non-fatal conditions the driver observed (e.g. idempotent no-op
        for a subset of fields). Empty list is normalized to ``None``.
    """

    success: bool
    changed: bool
    interface: str
    vendor: str | None = None
    execution_time_ms: float | None = None
    rollback_performed: bool | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses."""
        return {
            "success": self.success,
            "changed": self.changed,
            "interface": self.interface,
            "vendor": self.vendor,
            "execution_time_ms": self.execution_time_ms,
            "rollback_performed": self.rollback_performed,
            "warnings": list(self.warnings) if self.warnings is not None else None,
        }
