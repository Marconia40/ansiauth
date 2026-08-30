from __future__ import annotations

import re
from dataclasses import dataclass

_VLAN_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
_VLAN_NAME_ERROR = 'Invalid VLAN name. Only letters, numbers, ".", "_" and "-" are allowed.'


def _validate_vlan_id_range(vlan_id: int) -> None:
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")


def _validate_vlan_not_reserved(vlan_id: int) -> None:
    reserved = [1, 1002, 1003, 1004, 1005]
    if vlan_id in reserved:
        raise ValueError(f"VLAN {vlan_id} is reserved")


def _validate_vlan_name(name: str) -> None:
    if len(name) > 32:
        raise ValueError("VLAN name must not exceed 32 characters")
    if not _VLAN_NAME_RE.match(name):
        raise ValueError(_VLAN_NAME_ERROR)


def _validate_description(description: str) -> None:
    if len(description) > 64:
        raise ValueError("Description must not exceed 64 characters")
    if not _VLAN_NAME_RE.match(description):
        raise ValueError(_VLAN_NAME_ERROR)


@dataclass
class VLAN:
    """Domain representation of a VLAN — both the read-side data a driver
    returns and the write-side input used to create/delete/update one. One
    class, not a DTO/request pair: see ``docs/DEVICE_IMPLEMENTATION_PLAN.md``
    D7. Implementa el contrato ``RecursoGestionable`` (``validar``/
    ``reconciliar``/``aplicar``/``repositorio`` — formalizado en Fase 5,
    pero ya satisfecho por forma desde esta fase).

    Attributes
    ----------
    vlan_id:
        Numeric VLAN identifier (1–4094, excluding the reserved range).
    name:
        Human-readable label. Required for ``create_vlan``/
        ``update_vlan_description`` (see ``validar()``); left empty
        (``""``) when only ``vlan_id`` is needed, e.g. ``delete_vlan``.
    status:
        Optional platform-reported VLAN state (e.g. ``"active"``,
        ``"suspend"``). Populated only by parsers that surface this field;
        ``None`` when the platform does not expose it.
    device:
        Nombre del device al que pertenece esta VLAN — la identidad real de
        una VLAN persistida es (vlan_id, device), no vlan_id solo (switch-A
        y switch-B pueden tener cada uno su propia VLAN 100). Default ``""``
        porque ``VLAN`` se construye antes de saber a qué device va a
        aplicarse — quien la reparte se lo asigna (Fase 5,
        ``GroupOperationRunner``/``Orquestador``).
    eliminar:
        Marca intención de borrado — seteado por quien construye la ``VLAN``
        con esa intención (el router, en ``DELETE /vlans/{id}``). No es parte
        del contrato ``RecursoGestionable`` (la necesidad de delete es
        asimétrica entre recursos — ``Puerto`` no tiene delete real), es un
        campo propio que ``aplicar()`` mira internamente.

    Validation
    ----------
    ``__post_init__`` always validates ``vlan_id`` (range + not-reserved) —
    safe for both the read path (parsers already exclude reserved VLANs
    before constructing one) and the write path.

    ``name`` format is **not** validated automatically: parsers construct
    ``VLAN`` objects from whatever a real device reports, which can include
    characters (e.g. spaces in a VRP description) that the stricter
    user-input rules reject. Callers that need the name to satisfy those
    rules (``aplicar()`` en creación/rename) llaman ``validar()``
    explícito antes de usarlo.
    """

    vlan_id: int
    name: str = ""
    status: str | None = None
    device: str = ""
    eliminar: bool = False

    def __post_init__(self) -> None:
        _validate_vlan_id_range(self.vlan_id)
        _validate_vlan_not_reserved(self.vlan_id)

    def validar(self) -> None:
        """Validate ``name`` against the user-input format rules — satisface
        el contrato ``RecursoGestionable`` (Fase 5), llamado por
        ``Orquestador.ejecutar()`` sin condicionales (no sabe qué es una
        VLAN). Renombrado desde ``validate_name()`` (Fase 2) según lo ya
        previsto ahí: "el renombre es cosmético, se hace en Fase 5 al mismo
        tiempo que se cablea Orquestador".

        ``self.eliminar`` corta antes de validar ``name`` — corrección real
        encontrada en Fase 5 probando `DELETE /vlans/{id}` de punta a punta:
        una `VLAN(vlan_id=..., eliminar=True)` deja `name=""` a propósito
        (irrelevante para borrar), pero `Orquestador.ejecutar()` llama
        `validar()` siempre, sin importar la operación — sin este corte,
        cualquier delete fallaba con "Invalid VLAN name" antes de tocar el
        device. Mismo criterio que ``aplicar()`` ya usa para no mirar
        ``name`` en la rama de `eliminar`.

        Not run automatically by ``__post_init__`` — see the class
        docstring. Call this explicitly before using ``name`` in a write
        operation (create / rename).
        """
        if self.eliminar:
            return
        _validate_vlan_name(self.name)

    def reconciliar(self, device: "Device") -> dict:
        """Estado actual de esta VLAN en *device*, leído en vivo.
        Reemplaza vlan_execution_service.py: _capture_pre_state_vlan()."""
        vlans_actuales = device.driver.get_vlans(device, device.password)
        existente = next((v for v in vlans_actuales if v.vlan_id == self.vlan_id), None)
        return {
            "existed": existente is not None,
            "name": existente.name if existente is not None else None,
        }

    def aplicar(self, device: "Device") -> dict:
        """Aplica esta VLAN contra *device* — decide sola si es create, update,
        delete o no-op. Reemplaza vlan_execution_service.py: create_vlan_on_device()/
        delete_vlan()/update_vlan_description() (fusionadas: qué hacer lo decide
        el estado de ``self``, no 3 funciones separadas — save_config_on_device()
        no entra acá, es una operación a nivel device, no de una VLAN puntual).

        El dict devuelto siempre incluye "accion" — no lo pone el driver (que
        solo sabe de rc/stdout/stderr), lo agrega este método antes de retornar.
        Fase 3 (AuditListener) lo necesita para no perder la granularidad real
        de RF-AUD-02 detrás del evento genérico "recurso_aplicado" que despacha
        Orquestador."""
        pre_state = self.reconciliar(device)
        if self.eliminar:
            if not pre_state["existed"]:
                return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": "eliminar_vlan"}
            resultado = device.driver.delete_vlan(self.vlan_id, device, device.password)
            return {**resultado, "accion": "eliminar_vlan"}
        if pre_state["existed"] and pre_state["name"] == self.name:
            return {"rc": 0, "success": True, "changed": False, "noop": True, "accion": "crear_vlan"}
        if pre_state["existed"] and pre_state["name"] != self.name:
            self.validar()
            resultado = device.driver.update_vlan(self.vlan_id, self.name, device, device.password)
            return {**resultado, "accion": "actualizar_vlan"}
        self.validar()
        resultado = device.driver.create_vlan(self.vlan_id, self.name, device, device.password)
        return {**resultado, "accion": "crear_vlan"}

    def repositorio(self) -> str:
        return "vlan"

    def to_dict(self) -> dict:
        """Serialize to the ``{"vlan_id": int, "name": str}`` wire format.

        Used at API and JSON-storage boundaries (e.g. job ``pre_state``) to
        preserve full backward compatibility with callers that expect plain
        dicts.  The ``status`` field is intentionally excluded from the wire
        format until consuming layers are updated to handle it.

        Returns
        -------
        dict
            ``{"vlan_id": int, "name": str}``
        """
        return {"vlan_id": self.vlan_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict) -> VLAN:
        """Reconstruct a ``VLAN`` from a plain dict.

        Typically used when re-hydrating a record that was previously
        serialized via ``to_dict()`` and stored as JSON (e.g. job
        ``pre_state``).

        Parameters
        ----------
        data:
            Dict with at minimum ``vlan_id`` (int) and ``name`` (str) keys.
            An optional ``status`` key is also read if present.

        Returns
        -------
        VLAN
        """
        return cls(
            vlan_id=data["vlan_id"],
            name=data["name"],
            status=data.get("status"),
        )
