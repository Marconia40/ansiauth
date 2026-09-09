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
    ipv4_address_secondary: Optional[list[str]] = Field(None, description="Direcciones IPv4 secundarias en formato CIDR, o null si no hay ninguna.")
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
    -- asigna la dirección IPv4 primaria. Para limpiarla, ver
    ``DELETE /svis/ipv4``. Solo primaria -- la secundaria pasó a
    add/remove por dirección puntual, ver ``SVIIpv4SecondaryAddRequest``/
    ``SVIIpv4SecondaryRemoveRequest`` (ya no tiene sentido un flag
    "secondary" que "asigna un único valor" sobre lo que ahora es una
    lista)."""

    ipv4_address: str = Field(..., description="Dirección IPv4 en formato CIDR (ej. '10.10.10.11/24').")

    @field_validator("ipv4_address")
    @classmethod
    def _validar_ipv4(cls, v: str) -> str:
        return _validar_cidr(v, 4)


class SVIIpv4ClearRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/ipv4`` (RF-INTERV-03) -- limpia la
    IPv4 primaria. Solo primaria, mismo motivo que ``SVIIpv4UpdateRequest``."""


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
    campo en ``app.models.svi.SVI``, no se inventa nada nuevo.
    ``dhcp_relay_add``/``dhcp_relay_remove`` e ``ipv4_secondary_add``/
    ``ipv4_secondary_remove`` SI entran al batch -- mismo mecanismo que el
    resto (``VendorDriver.aplicar_lote()``), 1 sola conexion real junto
    con cualquier otro campo que venga en el mismo body. Cada par es
    mutuamente excluyente (mismo criterio que ``SVI``). El front limita a
    1 solo cambio encolado por Save para cada uno de los 2 pares, asi que
    nunca hace falta plegar 2 deltas contra la misma lectura previa (ver
    ``SVI._resolver_dhcp_relay_add``/``_resolver_dhcp_relay_remove`` y
    ``_resolver_ipv4_secondary_add``/``_resolver_ipv4_secondary_remove``).
    ``vlan_id`` es un segmento de la URL, no va en el body."""

    description: Optional[str] = Field(None, max_length=240)
    admin_up: Optional[bool] = None
    ipv4_address: Optional[str] = None
    ipv6_address: Optional[str] = None
    acl_in: Optional[str] = Field(None, description="Nombre/número de ACL, o '' para desasignar.")
    acl_out: Optional[str] = Field(None, description="Nombre/número de ACL, o '' para desasignar.")
    dhcp_relay_add: Optional[str] = Field(None, description="IP de 1 servidor DHCP relay a agregar.")
    dhcp_relay_remove: Optional[str] = Field(None, description="IP de 1 servidor DHCP relay a eliminar.")
    ipv4_secondary_add: Optional[str] = Field(None, description="1 dirección IPv4 secundaria (CIDR) a agregar.")
    ipv4_secondary_remove: Optional[str] = Field(None, description="1 dirección IPv4 secundaria (CIDR) a eliminar.")

    @field_validator("ipv4_address")
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

    @field_validator("dhcp_relay_add", "dhcp_relay_remove")
    @classmethod
    def _validar_dhcp_relay_ip(cls, v: "str | None") -> "str | None":
        if v:
            _validar_ip_plana(v)
        return v

    @field_validator("ipv4_secondary_add", "ipv4_secondary_remove")
    @classmethod
    def _validar_ipv4_secondary_cidr(cls, v: "str | None") -> "str | None":
        # A diferencia de dhcp_relay (IP plana), la secundaria sigue
        # necesitando CIDR/máscara -- mismo criterio que ipv4_address.
        if v:
            _validar_cidr(v, 4)
        return v

    @model_validator(mode="after")
    def _validar_dhcp_relay_exclusivo(self) -> "SVIBatchRequest":
        if self.dhcp_relay_add is not None and self.dhcp_relay_remove is not None:
            raise ValueError("cannot set dhcp_relay_add and dhcp_relay_remove in the same call")
        return self

    @model_validator(mode="after")
    def _validar_ipv4_secondary_exclusivo(self) -> "SVIBatchRequest":
        if self.ipv4_secondary_add is not None and self.ipv4_secondary_remove is not None:
            raise ValueError("cannot set ipv4_secondary_add and ipv4_secondary_remove in the same call")
        return self


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


class SVIIpv4SecondaryAddRequest(_SVITargetRequest):
    """Request body para ``POST /svis/ipv4-secondary`` (RF-INTERV-03) --
    agrega 1 dirección IPv4 secundaria, sin tocar las demás ya
    configuradas. Mismo criterio de verbo-en-el-método que
    ``SVIDhcpRelayAddRequest``. A diferencia de esa, la dirección va en
    CIDR (necesita máscara), no IP plana."""

    address: str = Field(..., description="Dirección IPv4 secundaria a agregar, en formato CIDR (ej. '10.10.10.12/24').")

    @field_validator("address")
    @classmethod
    def _validar_ip(cls, v: str) -> str:
        return _validar_cidr(v, 4)


class SVIIpv4SecondaryRemoveRequest(_SVITargetRequest):
    """Request body para ``DELETE /svis/ipv4-secondary`` (RF-INTERV-03) --
    elimina 1 dirección IPv4 secundaria puntual, sin tocar las demás."""

    address: str = Field(..., description="Dirección IPv4 secundaria a eliminar, en formato CIDR (ej. '10.10.10.12/24').")

    @field_validator("address")
    @classmethod
    def _validar_ip(cls, v: str) -> str:
        return _validar_cidr(v, 4)
