from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class GlobalConfigHostnameUpdateRequest(BaseModel):
    """Request body para ``PATCH /global-config/hostname`` (RF-GLOBAL-08).
    El device ya va en la URL (``/devices/{name}/global-config/hostname``),
    mismo criterio que el resto de esta app."""

    hostname: str = Field(..., min_length=1, max_length=63, description="Nuevo hostname del device.")


class GlobalConfigSnmpUpdateRequest(BaseModel):
    """Request body para ``PATCH /global-config/snmp`` (RF-GLOBAL-07).
    Los 3 campos son opcionales individualmente, pero ``community`` y
    ``permission`` deben venir juntos -- ambos vendors los fijan en 1 sola
    línea (ver ``GlobalConfig.validar()``)."""

    version: Optional[str] = Field(None, description="Versión SNMP a habilitar (ej. 'v2c'). Solo tiene efecto en VRP.")
    community: Optional[str] = Field(None, min_length=1, description="Community SNMP a configurar.")
    permission: Optional[Literal["RO", "RW"]] = Field(None, description="Permiso de la community.")

    @model_validator(mode="after")
    def _community_and_permission_together(self) -> "GlobalConfigSnmpUpdateRequest":
        if (self.community is None) != (self.permission is None):
            raise ValueError("'community' and 'permission' must be provided together")
        if self.version is None and self.community is None:
            raise ValueError("at least one of 'version' or 'community'+'permission' must be provided")
        return self


class GlobalConfigLogServersUpdateRequest(BaseModel):
    """Request body para ``PATCH /global-config/log-servers`` (RF-GLOBAL-09
    -- NTP/DNS/Log, el SRS los agrupa en 1 solo use case). Todos opcionales
    individualmente, al menos 1 debe venir."""

    ntp_server: Optional[str] = Field(None, min_length=1, description="Servidor NTP a configurar.")
    dns_server: Optional[str] = Field(None, min_length=1, description="Servidor DNS a configurar.")
    log_server: Optional[str] = Field(None, min_length=1, description="Servidor de Syslog a configurar.")
    log_level: Optional[str] = Field(None, min_length=1, description="Nivel de log a configurar.")

    @model_validator(mode="after")
    def _at_least_one(self) -> "GlobalConfigLogServersUpdateRequest":
        if not any((self.ntp_server, self.dns_server, self.log_server, self.log_level)):
            raise ValueError("at least one of ntp_server, dns_server, log_server, log_level must be provided")
        if self.log_level is not None and self.log_server is None:
            raise ValueError("'log_level' requires 'log_server' in the same request")
        return self


class GlobalConfigRouteAddRequest(BaseModel):
    """Request body para ``POST /global-config/routes`` (RF-GLOBAL-06).
    ``destination`` acepta un host dentro de la red (ej.
    ``"192.168.99.5/24"``) -- se normaliza a la dirección de red antes de
    aplicar/comparar (ver ``GlobalConfig._aplicar_route_add()``)."""

    destination: str = Field(..., min_length=1, description="Red destino en notación CIDR (ej. '192.168.99.0/24').")
    next_hop: str = Field(..., min_length=1, description="IP del next-hop.")


class GlobalConfigRead(BaseModel):
    """Wire-format representation de "Configuración Global" (SRS §3.4,
    RF-GLOBAL-01/02/03/04) returned by ``GET /global-config``.

    Mirrors ``app.models.global_config.GlobalConfig`` field-for-field
    (solo los campos de lectura -- los de escritura todavía no tienen
    endpoint, ver plan)."""

    device_version: Optional[str] = Field(
        None, description="Salida de 'show version'/'display version', o null si no se pudo leer.",
    )
    hostname: Optional[str] = Field(None, description="Hostname configurado en el device, o null.")
    snmp_enabled: Optional[bool] = Field(
        None, description="True si SNMP está habilitado en el device, False si no, null si no se pudo determinar.",
    )
    snmp_version: Optional[str] = Field(None, description="Versión de SNMP, o null.")
    snmp_community: Optional[str] = Field(
        None, description="Community SNMP configurada, o null (incluye el caso SNMP deshabilitado).",
    )
    snmp_permission: Optional[str] = Field(
        None, description="Permiso de la community ('RO'/'RW'), o null.",
    )
    ntp_server: Optional[str] = Field(None, description="Servidor NTP configurado, o null.")
    dns_server: Optional[str] = Field(None, description="Servidor DNS configurado, o null.")
    log_server: Optional[str] = Field(None, description="Servidor de Syslog configurado, o null.")
    log_level: Optional[str] = Field(None, description="Nivel de log configurado, o null.")
    routes: Optional[list[dict]] = Field(
        None, description="Tabla de ruteo (destino/next-hop/interfaz), o null si no se pudo leer.",
    )
    acls: Optional[list[str]] = Field(
        None, description="Nombres/números de las ACLs configuradas en el device, o null.",
    )
