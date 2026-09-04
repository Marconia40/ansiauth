from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


class GlobalConfigHostnameUpdateRequest(BaseModel):
    """Request body para ``PATCH /global-config/hostname`` (RF-GLOBAL-08).
    El device ya va en la URL (``/devices/{name}/global-config/hostname``),
    mismo criterio que el resto de esta app."""

    hostname: str = Field(..., min_length=1, max_length=63, description="Nuevo hostname del device.")


class GlobalConfigSnmpUpdateRequest(BaseModel):
    """Request body para ``PATCH /global-config/snmp`` (RF-GLOBAL-07).
    Todos los campos son opcionales individualmente -- ``community`` se
    fija siempre de solo lectura (ya no hay parámetro de permiso).
    ``trap_host``/``trap_version`` deben venir juntos. ``trap_source`` no
    tiene efecto confirmado en Huawei todavía (ver
    ``HuaweiVendor.set_snmp()``)."""

    version: Optional[str] = Field(None, description="Versión SNMP a habilitar (ej. 'v2c'). Solo tiene efecto en VRP.")
    community: Optional[str] = Field(None, min_length=1, description="Community SNMP a configurar (siempre solo lectura).")
    trap_source: Optional[str] = Field(None, min_length=1, description="Interfaz de administración usada como origen de los traps.")
    trap_host: Optional[str] = Field(None, min_length=1, description="IP destino de los traps SNMP.")
    trap_version: Optional[str] = Field(None, min_length=1, description="Versión SNMP del trap-host (ej. '2c'). Requerido junto con trap_host.")

    @model_validator(mode="after")
    def _validate(self) -> "GlobalConfigSnmpUpdateRequest":
        if (self.trap_host is None) != (self.trap_version is None):
            raise ValueError("'trap_host' and 'trap_version' must be provided together")
        if not any((self.version, self.community, self.trap_source, self.trap_host)):
            raise ValueError("at least one of version, community, trap_source, trap_host+trap_version must be provided")
        return self


class GlobalConfigLogServerAddRequest(BaseModel):
    """Request body para ``POST /global-config/log-servers`` (RF-GLOBAL-09,
    Log como endpoint propio). ``level`` es opcional y es un ajuste global
    del device (no por-host, en ninguno de los 2 vendors) -- viaja acá por
    conveniencia de API."""

    server: str = Field(..., min_length=1, description="IP del servidor de Syslog a agregar.")
    level: Optional[str] = Field(None, min_length=1, description="Nivel de severidad a configurar junto con este server.")


class GlobalConfigLogServerRemoveRequest(BaseModel):
    """Request body para ``DELETE /global-config/log-servers``."""

    server: str = Field(..., min_length=1, description="IP del servidor de Syslog a sacar.")


class GlobalConfigRouteAddRequest(BaseModel):
    """Request body para ``POST /global-config/routes`` (RF-GLOBAL-06) Y
    ``DELETE /global-config/routes`` (mismo shape para agregar/sacar, la
    diferencia es el verbo HTTP). ``destination`` acepta un host dentro de
    la red (ej. ``"192.168.99.5/24"``) -- se normaliza a la dirección de
    red antes de aplicar/comparar (ver ``GlobalConfig._aplicar_route_add()``/
    ``_aplicar_route_remove()``)."""

    destination: str = Field(..., min_length=1, description="Red destino en notación CIDR (ej. '192.168.99.0/24').")
    next_hop: str = Field(..., min_length=1, description="IP del next-hop.")


class GlobalConfigNtpAddRequest(BaseModel):
    """Request body para ``POST /global-config/ntp`` (RF-GLOBAL-09, NTP
    como endpoint propio). ``prefer`` es opcional y solo tiene efecto
    confirmado en Cisco."""

    server: str = Field(..., min_length=1, description="IP del servidor NTP a agregar.")
    prefer: Optional[bool] = Field(None, description="Marca este server como preferido (Cisco). Sin efecto confirmado en Huawei.")


class GlobalConfigNtpRemoveRequest(BaseModel):
    """Request body para ``DELETE /global-config/ntp``."""

    server: str = Field(..., min_length=1, description="IP del servidor NTP a sacar.")


class GlobalConfigDnsRequest(BaseModel):
    """Request body para ``POST /global-config/dns`` (RF-GLOBAL-09, DNS
    como endpoint propio). Sparse: exactamente 1 de ``server`` (agrega un
    DNS server, incremental) o ``domain_name`` (setea el domain-name del
    device, reemplaza el anterior)."""

    server: Optional[str] = Field(None, min_length=1, description="IP de un DNS server a agregar.")
    domain_name: Optional[str] = Field(None, min_length=1, description="Domain-name a configurar en el device.")

    @model_validator(mode="after")
    def _exactly_one(self) -> "GlobalConfigDnsRequest":
        if (self.server is None) == (self.domain_name is None):
            raise ValueError("exactly one of 'server' or 'domain_name' must be provided")
        return self


class GlobalConfigDnsRemoveRequest(BaseModel):
    """Request body para ``DELETE /global-config/dns`` -- solo saca
    servers (no hay "clear domain_name" en esta vuelta)."""

    server: str = Field(..., min_length=1, description="IP del DNS server a sacar.")


class GlobalConfigVersionRead(BaseModel):
    """Wire-format para ``GET /global-config/version`` (RF-GLOBAL-01, la
    mitad "y/o versión" del use case, separada de la config a pedido del
    usuario). Mismo cache que el resto -- no dispara una lectura nueva.
    ``software_version``/``model``/``serial_number``/``uptime`` son
    best-effort (ver ``parse_version_info()``, el formato de 'show
    version'/'display version' varía mucho incluso dentro del mismo
    vendor) -- ``raw`` siempre viene completo por si alguno da ``null``."""

    raw: Optional[str] = Field(None, description="Salida completa y sin procesar de 'show version'/'display version'.")
    software_version: Optional[str] = Field(None, description="Versión de software extraída (best-effort).")
    model: Optional[str] = Field(None, description="Modelo de hardware extraído (best-effort).")
    serial_number: Optional[str] = Field(None, description="Número de serie extraído (best-effort, no siempre presente en VRP).")
    uptime: Optional[str] = Field(None, description="Uptime tal cual lo reporta el device (best-effort).")


class GlobalConfigRead(BaseModel):
    """Wire-format representation de "Configuración Global" (SRS §3.4,
    RF-GLOBAL-01/02/03/04) returned by ``GET /global-config``.

    Mirrors ``app.models.global_config.GlobalConfig`` field-for-field
    (solo los campos de lectura -- los de escritura todavía no tienen
    endpoint, ver plan). ``device_version`` NO va acá a propósito -- RF-GLOBAL-01
    describe "consultar configuración general Y/O versión" como 2 cosas, y
    el usuario pidió separarlas: versión vive en su propio
    ``GET /global-config/version`` (``GlobalConfigVersionRead``), este
    endpoint queda enfocado solo en la config real del device."""

    running_config: Optional[list[str]] = Field(
        None,
        description=(
            "Dump completo de 'show running-config'/'display current-configuration' "
            "como lista de líneas (no 1 solo string), o null si no se pudo leer. "
            "Ya viene sin las líneas de metadata iniciales (ver "
            "GlobalConfig.running_config)."
        ),
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
