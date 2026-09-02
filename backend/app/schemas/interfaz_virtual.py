from __future__ import annotations

import ipaddress
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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


class InterfazVirtualRead(BaseModel):
    """Wire-format representation of a single virtual interface (SVI)
    returned by ``GET /interfaces-virtuales``.

    Mirrors ``app.models.interfaz_virtual.InterfazVirtual`` field-for-field
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


class _InterfazVirtualTargetRequest(BaseModel):
    """Target device+vlan_id compartido por cada request de escritura sobre
    una interfaz virtual puntual."""

    device: str = Field(..., min_length=1, description="Target device name.")
    vlan_id: int = Field(..., ge=1, le=4094, description="VLAN ID de la interfaz (1-4094).")


class InterfazVirtualCreateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``POST /interfaces-virtuales`` (RF-INTERV-01, +09).

    Crea la SVI de *vlan_id* en *device* -- la asociación a la VLAN (RF-INTERV-9)
    es implícita en la identidad (``vlan_id`` ES qué VLAN asocia). El endpoint
    valida que la VLAN ya exista en *device* antes de encolar. Si la interfaz
    ya existe, el sistema lo notifica como duplicado y no reintenta la
    creación (curso alternativo del SRS)."""

    description: Optional[str] = Field(
        default=None, max_length=240,
        description="Descripción opcional a aplicar al crear la interfaz (RF-INTERV-01).",
    )


class InterfazVirtualDeleteRequest(_InterfazVirtualTargetRequest):
    """Request body para ``POST /interfaces-virtuales/delete`` (RF-INTERV-02)."""


class InterfazVirtualAdminStateUpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/admin-state`` (RF-INTERV-03)."""

    enabled: bool = Field(..., description="Estado administrativo deseado -- True para activar, False para desactivar.")


class InterfazVirtualDescriptionUpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/description`` (RF-INTERV-08).

    Un ``description`` vacío limpia la descripción configurada."""

    description: str = Field(default="", max_length=240, description="Nueva descripción. Vacío limpia la descripción actual.")


class InterfazVirtualIpv4UpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/ipv4`` (RF-INTERV-03).

    ``ipv4_address`` vacío o ausente limpia la dirección configurada.
    ``secondary=true`` aplica sobre la IP secundaria en vez de la primaria
    -- requiere que ya exista una primaria en la interfaz (chequeado contra
    estado real del device, no algo que se pueda validar acá)."""

    ipv4_address: Optional[str] = Field(
        default=None,
        description="Dirección IPv4 en formato CIDR (ej. '10.10.10.11/24'). Vacío/null limpia la dirección actual.",
    )
    secondary: bool = Field(
        default=False,
        description="Si true, aplica sobre la IP secundaria (RF-INTERV-03) en vez de la primaria.",
    )

    @field_validator("ipv4_address")
    @classmethod
    def _validar_ipv4(cls, v: "str | None") -> "str | None":
        if not v:
            return v
        return _validar_cidr(v, 4)


class InterfazVirtualIpv6UpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/ipv6`` (RF-INTERV-04).

    ``ipv6_address`` vacío o ausente limpia la dirección configurada."""

    ipv6_address: Optional[str] = Field(
        default=None,
        description="Dirección IPv6 en formato CIDR (ej. '2001:db8::1/64'). Vacío/null limpia la dirección actual.",
    )

    @field_validator("ipv6_address")
    @classmethod
    def _validar_ipv6(cls, v: "str | None") -> "str | None":
        if not v:
            return v
        return _validar_cidr(v, 6)


class InterfazVirtualAclUpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/acl`` (RF-INTERV-05).

    Asigna (o limpia, si ``acl_name`` es vacío/null) una ACL que ya existe
    en el device en el sentido indicado -- no crea la ACL (RF-GLOBAL-04,
    aparte)."""

    direction: Literal["in", "out"] = Field(..., description="Sentido de la ACL -- 'in' o 'out'.")
    acl_name: Optional[str] = Field(
        default=None,
        description="Nombre/número de la ACL a asignar. Vacío/null limpia la ACL actual de ese sentido.",
    )


class InterfazVirtualDhcpRelayUpdateRequest(_InterfazVirtualTargetRequest):
    """Request body para ``PATCH /interfaces-virtuales/dhcp-relay`` (RF-INTERV-05).

    Incremental -- 1 server por llamada, no full-replace (así lo modela el
    SRS: "Acción: asignar/eliminar" sobre una dirección puntual). Exactamente
    uno de ``add``/``remove`` debe venir. El backend valida contra el
    estado real de la interfaz que la familia de IP (v4/v6) del server ya
    tenga su dirección correspondiente configurada, y evita duplicados/
    excede-el-límite."""

    add: Optional[str] = Field(default=None, description="IP de un servidor DHCP relay a agregar.")
    remove: Optional[str] = Field(default=None, description="IP de un servidor DHCP relay a eliminar.")

    @field_validator("add", "remove")
    @classmethod
    def _validar_ip(cls, v: "str | None") -> "str | None":
        if not v:
            return v
        return _validar_ip_plana(v)

    @model_validator(mode="after")
    def _exactamente_uno(self) -> "InterfazVirtualDhcpRelayUpdateRequest":
        if bool(self.add) == bool(self.remove):
            raise ValueError("exactly one of 'add' or 'remove' must be provided")
        return self
