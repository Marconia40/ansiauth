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


class GlobalConfigSnmpTrapHostRemoveRequest(BaseModel):
    """Request body para ``DELETE /global-config/snmp/trap-hosts``
    (RF-GLOBAL-07, contraparte de ``trap_host`` en ``PATCH /snmp``).
    ``community`` es requerida en los 2 vendors -- confirmado en vivo que
    ni IOS (``no snmp-server host {ip}`` solo sale "% Incomplete command",
    la community es el único completor que no depende de otro campo) ni
    VRP (exige la community EXACTA usada al agregar, queda cifrada al
    leerla de vuelta, no se puede recuperar del device) aceptan sacar un
    trap host sin ella."""

    host: str = Field(..., min_length=1, description="IP del trap host a sacar.")
    community: str = Field(
        ..., min_length=1,
        description="Community usada al agregar este trap host. Requerida en ambos vendors.",
    )


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
    ``software_version``/``model``/``uptime`` son best-effort (ver
    ``parse_version_info()``, el formato de 'show version'/'display
    version' varía mucho incluso dentro del mismo vendor). ``raw`` y
    ``serial_number`` se sacaron de la respuesta a pedido del usuario --
    ``parse_version_info()`` los sigue calculando internamente, solo no
    se exponen acá."""

    software_version: Optional[str] = Field(None, description="Versión de software extraída (best-effort).")
    model: Optional[str] = Field(None, description="Modelo de hardware extraído (best-effort).")
    uptime: Optional[str] = Field(None, description="Uptime tal cual lo reporta el device (best-effort).")


class GlobalConfigRunningConfigRead(BaseModel):
    """Wire-format para ``GET /global-config/running-config`` -- separado
    del resto a pedido del usuario (el dump completo es lo más pesado de
    la respuesta y conceptualmente distinto de "los ajustes puntuales que
    configuramos", que quedan en ``GET /global-config/``). Mismo cache que
    el resto -- no dispara una lectura nueva."""

    running_config: Optional[list[str]] = Field(
        None,
        description=(
            "Dump completo de 'show running-config'/'display current-configuration' "
            "como lista de líneas (no 1 solo string), o null si no se pudo leer. "
            "Ya viene sin las líneas de metadata iniciales (ver "
            "GlobalConfig.running_config)."
        ),
    )


class GlobalConfigSnmpInfo(BaseModel):
    """Sub-objeto SNMP de ``GET /global-config`` -- agrupado a pedido del
    usuario (la respuesta plana con 4 campos ``snmp_*`` sueltos mezclados
    con hostname/routes/acls "no se veía bien")."""

    enabled: Optional[bool] = Field(
        None, description="True si SNMP está habilitado en el device, False si no, null si no se pudo determinar.",
    )
    version: Optional[str] = Field(
        None,
        description=(
            "Versión SNMP -- en VRP viene de 'snmp-agent sys-info version' (puede ser "
            "más de 1, ej. 'v2c v3'); en IOS clásico no hay ajuste de versión "
            "independiente, se toma de la línea de trap-host si hay una configurada. "
            "Null si no se pudo determinar ninguna."
        ),
    )
    community: Optional[str] = Field(
        None,
        description=(
            "Community SNMP configurada, o null (incluye el caso SNMP deshabilitado). "
            "En Huawei siempre da null aunque haya una configurada -- VRP la guarda cifrada, "
            "no se puede revertir del lado del parser."
        ),
    )
    permission: Optional[str] = Field(None, description="Permiso de la community ('RO'/'RW'), o null.")
    trap_hosts: Optional[list[str]] = Field(
        None,
        description=(
            "En Cisco, IPs destino reales de los traps SNMP ('snmp-server host', puede haber "
            "más de 1 línea). En Huawei no hay trap-host leíble ('snmp-agent target-host' no "
            "tiene lectura implementada) -- se toman en cambio TODOS los hosts permitidos por "
            "la ACL que 'snmp-agent acl {nombre}' ata al agente SNMP (misma ACL que aparece "
            "en 'acls'). Null si no hay ninguno configurado o no se pudo resolver."
        ),
    )


class GlobalConfigNtpInfo(BaseModel):
    """Sub-objeto NTP -- ver nota de ``GlobalConfigSnmpInfo``."""

    servers: Optional[list[str]] = Field(
        None, description="Servidores NTP configurados (todos, no solo el primero), o null.",
    )


class GlobalConfigDnsInfo(BaseModel):
    """Sub-objeto DNS -- mismo criterio que ``GlobalConfigNtpInfo``."""

    servers: Optional[list[str]] = Field(
        None, description="Servidores DNS configurados (todos, no solo el primero), o null.",
    )


class GlobalConfigLoggingInfo(BaseModel):
    """Sub-objeto de logging -- mismo criterio que ``GlobalConfigNtpInfo``."""

    servers: Optional[list[str]] = Field(
        None, description="Servidores de Syslog configurados (todos, no solo el primero), o null.",
    )
    level: Optional[str] = Field(None, description="Nivel de log configurado, o null.")


class GlobalConfigAclInfo(BaseModel):
    """1 ACL dentro de ``GlobalConfigRead.acls`` -- reshape pedido por el
    usuario tras ver la respuesta con ``acls`` como solo nombres ("la acl
    debería especificar el contenido de cada una"). ``rules`` queda como
    líneas crudas tal cual las imprime el device (no se re-estructura cada
    regla en source/dest/protocolo/etc. -- alcance explícitamente pedido,
    ver ``list_acls()`` en cada driver)."""

    name: str = Field(..., description="Nombre o número de la ACL.")
    type: Optional[str] = Field(
        None,
        description=(
            "Tipo de ACL tal cual lo reporta el device -- 'standard'/'extended' en Cisco, "
            "'basic'/'advanced'/'ethernet frame'/'user' en Huawei."
        ),
    )
    rules: list[str] = Field(
        default_factory=list, description="Reglas de la ACL, 1 línea cruda por regla (formato vendor-específico).",
    )


class GlobalConfigAclRuleEndpoint(BaseModel):
    """``source``/``destination`` de una regla de ACL (RF-GLOBAL-05) --
    sparse, exactamente 1 de los 3. ``network`` acepta CIDR (ej.
    '172.28.138.0/24'), no wildcard cruda -- el driver de cada vendor
    calcula la wildcard mask (Cisco) o el formato que corresponda (Huawei,
    sin confirmar todavía) a partir de la CIDR."""

    any: Optional[bool] = Field(None, description="True para 'any' (cualquier origen/destino).")
    host: Optional[str] = Field(None, min_length=1, description="1 solo host (ej. '192.0.2.5').")
    network: Optional[str] = Field(None, min_length=1, description="Red en notación CIDR (ej. '172.28.138.0/24').")

    @model_validator(mode="after")
    def _exactly_one(self) -> "GlobalConfigAclRuleEndpoint":
        provided = [v for v in (self.any, self.host, self.network) if v not in (None, False)]
        if len(provided) != 1:
            raise ValueError("exactly one of 'any', 'host', 'network' must be provided")
        return self


class GlobalConfigAclRulePort(BaseModel):
    """Puerto/rango de una regla de ACL -- solo ``eq``/``range`` por ahora
    (alcance confirmado con el usuario; ``gt``/``lt``/``neq`` quedan
    afuera de esta vuelta). Sin restricción de protocolo del lado del
    schema (ej. mandar esto con protocol='ip') -- si el device lo
    rechaza, el error real queda visible en el job, mismo criterio que el
    resto de la app."""

    operator: Literal["eq", "range"] = Field(..., description="'eq' (puerto/servicio único) o 'range' (rango).")
    value: str = Field(..., min_length=1, description="Puerto/servicio (ej. 'bootps', '443') o inicio del rango.")
    value2: Optional[str] = Field(None, min_length=1, description="Fin del rango. Requerido solo si operator='range'.")

    @model_validator(mode="after")
    def _range_needs_value2(self) -> "GlobalConfigAclRulePort":
        if self.operator == "range" and not self.value2:
            raise ValueError("'value2' is required when operator='range'")
        if self.operator == "eq" and self.value2:
            raise ValueError("'value2' is only valid when operator='range'")
        return self


class GlobalConfigAclRule(BaseModel):
    """1 regla de ACL (RF-GLOBAL-05) -- shape vendor-agnóstico, cada
    driver la traduce a su sintaxis real (ver
    ``CiscoVendor._formatear_regla_acl()``). ``protocol`` es un string
    libre (ej. 'ip'/'tcp'/'udp'/'icmp'/número) sin enum -- mismo criterio
    que ``logging.level``/``snmp.version``, el device es la autoridad de
    qué protocolo es válido, no esta app."""

    action: Literal["permit", "deny"]
    protocol: str = Field(..., min_length=1)
    source: GlobalConfigAclRuleEndpoint
    destination: GlobalConfigAclRuleEndpoint
    port: Optional[GlobalConfigAclRulePort] = Field(
        None, description="Puerto/rango opcional (típicamente solo tiene sentido con tcp/udp).",
    )


class GlobalConfigAclCreateRequest(BaseModel):
    """Request body para ``POST /global-config/acls`` (RF-GLOBAL-05).
    Crea la ACL si no existe; si ya existe, agrega las reglas nuevas
    (mismo comando sirve para ambos casos -- entrar al contexto de una
    ACL extended/advanced la crea si no estaba). No-op por regla: las que
    ya estén configuradas tal cual no se re-envían (ver
    ``GlobalConfig._aplicar_acl_create()``)."""

    name: str = Field(..., min_length=1, description="Nombre de la ACL a crear o extender.")
    rules: list[GlobalConfigAclRule] = Field(..., min_length=1, description="Reglas a agregar (al menos 1).")


class GlobalConfigAclRuleRemoveRequest(BaseModel):
    """Request body para ``DELETE /global-config/acls/rules`` -- mismo
    shape que ``GlobalConfigAclCreateRequest``, saca las reglas indicadas
    (no-op para las que no estén configuradas)."""

    name: str = Field(..., min_length=1, description="Nombre de la ACL de la que sacar reglas.")
    rules: list[GlobalConfigAclRule] = Field(..., min_length=1, description="Reglas a sacar (al menos 1).")


class GlobalConfigAclDeleteRequest(BaseModel):
    """Request body para ``DELETE /global-config/acls`` -- borra la ACL
    completa (todas sus reglas)."""

    name: str = Field(..., min_length=1, description="Nombre de la ACL a borrar completa.")


class GlobalConfigRead(BaseModel):
    """Wire-format representation de "Configuración Global" (SRS §3.4,
    RF-GLOBAL-01/02/03/04) returned by ``GET /global-config``.

    Los campos relacionados van agrupados en sub-objetos (``snmp``/``ntp``/
    ``dns``/``logging``) en vez de todos sueltos al mismo nivel que
    ``hostname``/``routes``/``acls`` -- reshape pedido por el usuario tras
    ver la respuesta plana original. ``device_version``/``running_config``
    NO van acá a propósito -- versión vive en su propio ``GET
    /global-config/version`` (RF-GLOBAL-01 describe "consultar
    configuración general Y/O versión" como 2 cosas) y el dump completo de
    running-config vive en su propio ``GET /global-config/running-config``
    (es lo más pesado de la respuesta, y conceptualmente distinto de "los
    ajustes puntuales" que sí quedan acá) -- ambos separados a pedido del
    usuario."""

    hostname: Optional[str] = Field(None, description="Hostname configurado en el device, o null.")
    snmp: GlobalConfigSnmpInfo = Field(default_factory=GlobalConfigSnmpInfo)
    ntp: GlobalConfigNtpInfo = Field(default_factory=GlobalConfigNtpInfo)
    dns: GlobalConfigDnsInfo = Field(default_factory=GlobalConfigDnsInfo)
    logging: GlobalConfigLoggingInfo = Field(default_factory=GlobalConfigLoggingInfo)
    routes: Optional[list[dict]] = Field(
        None,
        description=(
            "Tabla de ruteo (destino/next_hop/interfaz), o null si no se pudo leer. "
            "next_hop/interfaz son individualmente null según el tipo de ruta -- una "
            "conectada no tiene next_hop (por definición, sale directo por la interfaz), "
            "y una ruta estática vía next-hop puede no traer interfaz si el device no la "
            "resuelve en el 'show ip route'/'display ip routing-table' (confirmado en vivo, "
            "no es un gap del parser). Incluye también rutas estáticas configuradas cuyo "
            "next_hop no es alcanzable (no aparecen en la tabla activa, pero SÍ en el "
            "running-config -- se leen de ahí también) para que se puedan ver y borrar "
            "desde la interfaz aunque no estén activas."
        ),
    )
    acls: Optional[list[GlobalConfigAclInfo]] = Field(
        None, description="ACLs configuradas en el device, con sus reglas, o null.",
    )
