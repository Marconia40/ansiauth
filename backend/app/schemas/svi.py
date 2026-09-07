from __future__ import annotations

import ipaddress
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _validar_cidr(value: str, version: int) -> str:
    interfaz = ipaddress.ip_interface(value)
    if interfaz.version != version:
        raise ValueError(f"expected an IPv{version} address, got {value!r}")
    return value


def _validar_ip_plana(value: str) -> str:
    """DHCP relay (RF-INTERV-05): SRS pide IP plana (``X.X.X.X`` /
    ``X:X::X:X``), sin prefix -- a diferencia de ipv4/ipv6 de la propia
    interfaz, que sí van en CIDR."""
    ipaddress.ip_address(value)
    return value


class SVIRead(BaseModel):
    """Wire-format representation of a single virtual interface (SVI)
    returned by ``GET /svis``.

    Mirrors ``app.models.svi.SVI`` field-for-field
    (minus ``eliminar``, que es intención de escritura, no estado)."""

    vlan_id: int = Field(..., description="VLAN ID -- identidad real de la interfaz (Vlan{id}/Vlanif{id}).")
    description: Optional[str] = Field(None, description="Descripción configurada, o null si no hay.")
    admin_up: Optional[bool] = Field(None, description="Estado administrativo (True = no shutdown).")
    operational_up: Optional[bool] = Field(None, description="Estado operacional real reportado por el device.")
    ipv4_address: Optional[str] = Field(None, description="Dirección IPv4 primaria en formato CIDR, o null si no configurada.")
    ipv4_address_secondary: Optional[str] = Field(None, description="Dirección IPv4 secundaria en formato CIDR, o null si no hay.")
    ipv6_address: Optional[str] = Field(None, description="Dirección IPv6 en formato CIDR, o null si no configurada.")
    acl_in: Optional[str] = Field(None, description="Nombre/número de ACL aplicada en sentido entrante, o null.")
    acl_out: Optional[str] = Field(None, description="Nombre/número de ACL aplicada en sentido saliente, o null.")
    dhcp_relay_servers: Optional[list[str]] = Field(None, description="Lista de IPs de DHCP relay, o null si no hay ninguna.")


class _SVITargetRequest(BaseModel):
    """Target vlan_id compartido por cada request de escritura sobre una
    SVI puntual. El device ya no va en el body -- es un
    segmento de la URL (``/devices/{name}/svis/...``),
    mismo criterio que los endpoints de refresh."""

    vlan_id: int = Field(..., ge=1, le=4094, description="VLAN ID de la interfaz (1-4094).")


class SVICreateRequest(_SVITargetRequest):
    """Request body para ``POST /svis`` (RF-INTERV-01, +09).

    Crea la SVI de *vlan_id* en *device* -- la asociación a la VLAN (RF-INTERV-9)
    es implícita en la identidad (``vlan_id`` ES qué VLAN asocia). El endpoint
    valida que la VLAN ya exista en *device* antes de encolar. Si la interfaz
    ya existe, el sistema lo notifica como duplicado y no reintenta la
    creación (curso alternativo del SRS)."""

    description: Optional[str] = Field(
        default=None, max_length=240,
        description="Descripción opcional a aplicar al crear la interfaz (RF-INTERV-01).",
    )


class SVIDeleteRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/`` (RF-INTERV-02)."""


class SVIAdminStateUpdateRequest(_SVITargetRequest):
    """Request body para ``PATCH /svis/admin-state`` (RF-INTERV-03)."""

    enabled: bool = Field(..., description="Estado administrativo deseado -- True para activar, False para desactivar.")


class SVIDescriptionUpdateRequest(_SVITargetRequest):
    """Request body para ``PATCH /svis/description``
    (RF-INTERV-08) -- asigna una descripción. Para limpiarla, ver
    ``DELETE /svis/description`` (verbo explícito en vez de
    inferir "limpiar" de un valor vacío -- mismo criterio que DHCP relay,
    ver ``SVIDhcpRelayAddRequest``)."""

    description: str = Field(..., min_length=1, max_length=240, description="Nueva descripción.")


class SVIDescriptionClearRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/description`` (RF-INTERV-08)."""


class SVIIpv4UpdateRequest(_SVITargetRequest):
    """Request body para ``PATCH /svis/ipv4`` (RF-INTERV-03)
    -- asigna una dirección IPv4. Para limpiarla, ver
    ``DELETE /svis/ipv4``. ``secondary=true`` aplica sobre
    la IP secundaria en vez de la primaria -- requiere que ya exista una
    primaria en la interfaz (chequeado contra estado real del device, no
    algo que se pueda validar acá)."""

    ipv4_address: str = Field(..., description="Dirección IPv4 en formato CIDR (ej. '10.10.10.11/24').")
    secondary: bool = Field(
        default=False,
        description="Si true, aplica sobre la IP secundaria (RF-INTERV-03) en vez de la primaria.",
    )

    @field_validator("ipv4_address")
    @classmethod
    def _validar_ipv4(cls, v: str) -> str:
        return _validar_cidr(v, 4)


class SVIIpv4ClearRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/ipv4`` (RF-INTERV-03)."""

    secondary: bool = Field(
        default=False,
        description="Si true, limpia la IP secundaria en vez de la primaria.",
    )


class SVIIpv6UpdateRequest(_SVITargetRequest):
    """Request body para ``PATCH /svis/ipv6`` (RF-INTERV-04)
    -- asigna una dirección IPv6. Para limpiarla, ver
    ``DELETE /svis/ipv6``."""

    ipv6_address: str = Field(..., description="Dirección IPv6 en formato CIDR (ej. '2001:db8::1/64').")

    @field_validator("ipv6_address")
    @classmethod
    def _validar_ipv6(cls, v: str) -> str:
        return _validar_cidr(v, 6)


class SVIIpv6ClearRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/ipv6`` (RF-INTERV-04)."""


class SVIAclUpdateRequest(_SVITargetRequest):
    """Request body para ``PATCH /svis/acl`` (RF-INTERV-05)
    -- asigna una ACL que ya existe en el device al sentido indicado, no la
    crea (RF-GLOBAL-04, aparte). Para desasignarla, ver
    ``DELETE /svis/acl``."""

    direction: Literal["in", "out"] = Field(..., description="Sentido de la ACL -- 'in' o 'out'.")
    acl_name: str = Field(..., min_length=1, description="Nombre/número de la ACL a asignar.")


class SVIAclClearRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/acl`` (RF-INTERV-05)."""

    direction: Literal["in", "out"] = Field(..., description="Sentido de la ACL a desasignar -- 'in' o 'out'.")


class SVIBatchRequest(BaseModel):
    """Body de ``PATCH /devices/{name}/svis/{vlan_id}/batch`` -- TODOS los
    campos opcionales juntos en 1 solo objeto (a diferencia de cada
    endpoint individual, que exige exactamente 1). ``None`` = no tocar,
    ``""`` = limpiar (mismo criterio que el resto de esta clase),
    cualquier otro valor = asignar -- mismo significado que ya tiene cada
    campo en ``app.models.svi.SVI``, no se inventa nada nuevo. DHCP relay
    queda afuera a propósito (ver ``SVI.resolver_paso()``) -- sigue siendo
    "fire inmediato" vía ``POST``/``DELETE /svis/dhcp-relay``, no entra al
    batch. ``vlan_id`` es un segmento de la URL, no va en el body."""

    description: Optional[str] = Field(None, max_length=240)
    admin_up: Optional[bool] = None
    ipv4_address: Optional[str] = None
    ipv4_address_secondary: Optional[str] = None
    ipv6_address: Optional[str] = None
    acl_in: Optional[str] = Field(None, description="Nombre/número de ACL, o '' para desasignar.")
    acl_out: Optional[str] = Field(None, description="Nombre/número de ACL, o '' para desasignar.")

    @field_validator("ipv4_address", "ipv4_address_secondary")
    @classmethod
    def _validar_ipv4_o_vacio(cls, v: "str | None") -> "str | None":
        if v:
            _validar_cidr(v, 4)
        return v

    @field_validator("ipv6_address")
    @classmethod
    def _validar_ipv6_o_vacio(cls, v: "str | None") -> "str | None":
        if v:
            _validar_cidr(v, 6)
        return v


class SVIDhcpRelayAddRequest(_SVITargetRequest):
    """Request body para ``POST /svis/dhcp-relay``
    (RF-INTERV-05) -- agrega 1 server, sin tocar los demás ya configurados.

    El verbo POST ya expresa la acción (alta) -- a diferencia de un único
    endpoint con un campo ``add``/``remove`` en el body (lo que se
    descartó: el método HTTP no dice nada sobre qué hace, y hace falta un
    validador cruzado para exigir exactamente uno de los dos)."""

    server: str = Field(..., description="IP de un servidor DHCP relay a agregar.")

    @field_validator("server")
    @classmethod
    def _validar_ip(cls, v: str) -> str:
        return _validar_ip_plana(v)


class SVIDhcpRelayRemoveRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/dhcp-relay``
    (RF-INTERV-05) -- elimina 1 server, sin tocar los demás."""

    server: str = Field(..., description="IP de un servidor DHCP relay a eliminar.")

    @field_validator("server")
    @classmethod
    def _validar_ip(cls, v: str) -> str:
        return _validar_ip_plana(v)
