from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, unique=True, index=True)
    email = Column(String, nullable=True, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Global system-wide privilege. Fully replaces the pre-MSP ``role``
    # column. Per-scope authorization is expressed through
    # ``role_assignments``.
    is_system_admin = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_user_username"),
        UniqueConstraint("email", name="uq_user_email"),
    )


class DeviceModel(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True, index=True)
    host = Column(String, nullable=False)
    vendor = Column(String, nullable=False)
    platform = Column(String, nullable=True)  # nullable for backward compat with existing rows
    username = Column(String, nullable=False)
    encrypted_password = Column(String, nullable=False)
    # "password" (default) o "key" -- ver Device._VALID_AUTH_METHODS. Server
    # default cubre las filas existentes sin migración de datos.
    auth_method = Column(String, nullable=False, server_default="password")
    # Solo poblado cuando auth_method == "key" -- ver Device.private_key.
    encrypted_private_key = Column(Text, nullable=True)
    # Every device belongs to exactly one group; the site is derived through
    # ``device_group.site``.
    device_group_id = Column(
        Integer,
        ForeignKey(
            "device_groups.id", use_alter=True, name="fk_devices_device_group_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # Cache-first read model: los GET de VLAN/ports leen de
    # device_vlans/device_ports en vez de golpear el equipo cada vez. Estas
    # 4 columnas son la metadata "cuándo fue la última sync exitosa" y
    # "el último intento falló con esto" por-recurso (separados porque
    # VLAN y ports se sincronizan por caminos distintos). Nullable: un
    # device recién dado de alta arranca en NULL hasta que sync_device_task
    # completa.
    vlans_synced_at = Column(DateTime(timezone=True), nullable=True)
    vlans_sync_error = Column(Text, nullable=True)
    ports_synced_at = Column(DateTime(timezone=True), nullable=True)
    ports_sync_error = Column(Text, nullable=True)
    svis_synced_at = Column(DateTime(timezone=True), nullable=True)
    svis_sync_error = Column(Text, nullable=True)
    global_config_synced_at = Column(DateTime(timezone=True), nullable=True)
    global_config_sync_error = Column(Text, nullable=True)
    # ARP/MAC quedaron con su propio scope de sync a pedido del usuario --
    # no van en "all" (alta de device) porque pueden traer muchísima info
    # y no hacen falta para ninguna escritura; solo se sincronizan cuando
    # alguien pide explícitamente ``POST .../arp-mac/refresh``.
    arp_mac_synced_at = Column(DateTime(timezone=True), nullable=True)
    arp_mac_sync_error = Column(Text, nullable=True)
    # Logs -- mismo criterio que ARP/MAC arriba, scope de sync propio,
    # afuera de "all".
    logs_synced_at = Column(DateTime(timezone=True), nullable=True)
    logs_sync_error = Column(Text, nullable=True)

    device_group = relationship(
        "DeviceGroupModel",
        foreign_keys=[device_group_id],
        uselist=False,
    )

    __table_args__ = (UniqueConstraint("name", name="uq_device_name"),)


class DeviceVlanModel(Base):
    """MSP: Fase 2 de la migración a FINAL_ARCHITECTURE.md — Repository[VLAN].

    PK compuesta real (``vlan_id``, ``device``), sin ``id`` autoincrement
    separado: la identidad real de una VLAN persistida es (vlan_id, device),
    no vlan_id solo (switch-A y switch-B pueden tener cada uno su propia VLAN
    100). Necesario para que ``session.merge()`` reconozca la fila existente
    de forma nativa en ``Repository[T].add()`` — ver FASE_1.md, Repository[T].
    """

    __tablename__ = "device_vlans"

    vlan_id = Column(Integer, primary_key=True)
    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    name = Column(String, nullable=False)


class DevicePortModel(Base):
    """MSP: Fase 2 de la migración a FINAL_ARCHITECTURE.md — Repository[Puerto].

    PK compuesta real (``interface``, ``device``), mismo criterio que
    ``DeviceVlanModel``. Guarda el estado del puerto -- tanto el
    deseado/aplicado (description, admin_up, mode, access_vlan, allowed_vlans,
    poe_enabled) como el observado read-only (operational_up, speed, duplex)
    para que los GET cache-first no pierdan esa info entre refresh y refresh.
    ``allowed_vlan_operation`` sigue afuera: es instrucción de la llamada,
    no atributo persistente del puerto.
    """

    __tablename__ = "device_ports"

    interface = Column(String, primary_key=True)
    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    description = Column(String, nullable=True)
    admin_up = Column(Boolean, nullable=True)
    mode = Column(String, nullable=True)
    access_vlan = Column(Integer, nullable=True)
    allowed_vlans = Column(JSON, nullable=True)  # lista de int
    poe_enabled = Column(Boolean, nullable=True)
    # storm_control_threshold es percent (0-100). None con enabled=True
    # significa "prendido pero en pps/bps" -- config preexistente al
    # sistema, nuestro write siempre produce percent-form.
    storm_control_enabled = Column(Boolean, nullable=True)
    storm_control_threshold = Column(Float, nullable=True)
    # "filter"/"shutdown" -- ver Puerto.storm_control_action. Leído del
    # running-config/current-configuration (no es estado operacional).
    storm_control_action = Column(String, nullable=True)
    storm_control_trap = Column(Boolean, nullable=True)
    # Read-only, provienen del getter del driver, no de escrituras del usuario.
    operational_up = Column(Boolean, nullable=True)
    speed = Column(String, nullable=True)
    duplex = Column(String, nullable=True)


class DeviceSVIModel(Base):
    """RF-INTERV-* — Repository[SVI]. PK compuesta real
    (``vlan_id``, ``device``), mismo criterio que ``DeviceVlanModel``/
    ``DevicePortModel``. Doble función igual que esas 2 tablas: destino del
    tracking-write de Orquestador tras una escritura exitosa, y cache que
    sirve ``GET /svis`` (front-data, cache-first)."""

    __tablename__ = "device_svis"

    vlan_id = Column(Integer, primary_key=True)
    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    description = Column(String, nullable=True)
    admin_up = Column(Boolean, nullable=True)
    ipv4_address = Column(String, nullable=True)
    ipv4_address_secondary = Column(JSON, nullable=True)  # lista de str
    ipv6_address = Column(String, nullable=True)
    acl_in = Column(String, nullable=True)
    acl_out = Column(String, nullable=True)
    dhcp_relay_servers = Column(JSON, nullable=True)  # lista de str
    # Read-only, viene del getter del driver.
    operational_up = Column(Boolean, nullable=True)


class DeviceGlobalConfigModel(Base):
    """RF-GLOBAL-* (SRS §3.4) — Repository[GlobalConfig]. Singleton por
    device -- a diferencia de VLAN/Puerto/SVI, acá la identidad ES el
    device solo (``device`` es la PK completa, no compuesta), no hay
    colección de sub-elementos. ``routes``/``acls`` van como JSON (no como
    tablas propias): RF-GLOBAL-06 solo pide alta de rutas, RF-GLOBAL-05
    hace full-replace de las reglas de una ACL al modificarla -- ninguna de
    las 2 necesita PK propia por elemento, mismo criterio que
    ``dhcp_relay_servers`` en ``DeviceSVIModel``."""

    __tablename__ = "device_global_config"

    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    hostname = Column(String, nullable=True)
    running_config = Column(Text, nullable=True)  # RF-GLOBAL-01, dump completo de show running-config/display current-configuration
    device_version = Column(String, nullable=True)
    snmp_enabled = Column(Boolean, nullable=True)
    snmp_version = Column(String, nullable=True)
    snmp_community = Column(String, nullable=True)
    snmp_permission = Column(String, nullable=True)
    snmp_trap_hosts = Column(JSON, nullable=True)  # lista de str. Cisco: IPs de "snmp-server host"; Huawei: hosts permitidos por la ACL atada al agente ("snmp-agent acl"), target-host en sí no tiene lectura
    ntp_servers = Column(JSON, nullable=True)  # lista de str, puede haber más de 1 configurado
    dns_servers = Column(JSON, nullable=True)  # lista de str, puede haber más de 1 configurado
    log_servers = Column(JSON, nullable=True)  # lista de str, puede haber más de 1 configurado
    log_level = Column(String, nullable=True)
    routes = Column(JSON, nullable=True)  # lista de dict (destino/mask/next-hop/interfaz)
    acls = Column(JSON, nullable=True)  # lista de dict (nombre/tipo/reglas)


class DeviceArpMacModel(Base):
    """Repository[ArpMacTables]. Separada de ``DeviceGlobalConfigModel`` a
    pedido del usuario -- ARP/MAC no hace falta para ninguna escritura
    (no pasa por ``reconciliar()``) y "puede traer muchísima info", así
    que tiene su propio sync específico (``DeviceSyncService.sync_arp_mac()``,
    scope ``"arp_mac"`` -- NO forma parte de ``sync_device_task(..., "all")``,
    hay que pedirlo explícito vía ``POST .../arp-mac/refresh``). Estar en
    tabla propia evita además que un sync de global_config (que ya no
    trae estos datos) pise estas columnas con NULL -- ``Repository.add()``
    hace ``session.merge()`` del objeto completo, así que compartir fila
    con algo sincronizado por un camino distinto es un riesgo real, no
    solo una cuestión de prolijidad."""

    __tablename__ = "device_arp_mac"

    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    arp_table = Column(JSON, nullable=True)  # lista de dict (ip/mac/interface/vlan/type/age), tabla completa sin filtrar
    mac_table = Column(JSON, nullable=True)  # lista de dict (mac/vlan/interface/type), tabla completa sin filtrar


class DeviceLogsModel(Base):
    """Repository[DeviceLogs]. Mismo criterio que ``DeviceArpMacModel`` --
    tabla/scope de sync propios (``"logs"``, tampoco forma parte de
    ``"all"``), pedido por el usuario "de la misma forma que las tablas
    mac y arp". ``log_output`` queda Text (no JSON) -- mismo criterio que
    ``DeviceGlobalConfigModel.running_config``, es texto crudo multi-línea,
    no filas tabulares."""

    __tablename__ = "device_logs"

    device = Column(
        String,
        ForeignKey("devices.name", ondelete="CASCADE"),
        primary_key=True,
    )
    log_output = Column(Text, nullable=True)


class JobModel(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, nullable=False, unique=True, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    # MSP: Fase 4 de la migración a FINAL_ARCHITECTURE.md -- GroupJob.operation
    # (ej. "create_vlan") es un dato real que GET /group-jobs/{id} expone hoy;
    # sin esta columna, JobRepository.resumen_de_grupo() (Fase 4 A3) no tiene
    # de dónde sacarlo al eliminar GroupJob (A2).
    operation = Column(String, nullable=True)
    playbook = Column(String, nullable=True)
    device = Column(String, nullable=True)
    parameters = Column(JSON, nullable=True)
    # Frase legible de la intención del request (ej. "Add route
    # 192.168.100.0/24 -> 10.10.100.1") -- Job.parameters_summary. Faltaba
    # esta columna desde que el campo se agregó (bug real encontrado y
    # cerrado en la misma vuelta que agrega error_summary abajo): sin ella
    # ni _to_orm() ni _to_domain() podían mapearlo, así que nunca
    # sobrevivía un roundtrip por la base.
    parameters_summary = Column(Text, nullable=True)
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=3)
    rollback_performed = Column(Boolean, nullable=False, default=False)
    rollback_success = Column(Boolean, nullable=True)
    pre_state = Column(JSON, nullable=True)
    last_error = Column(Text, nullable=True)
    # Clasificación amigable del error final -- ver docstring en
    # ``app/models/job.py`` para el criterio. Los 3 son opcionales: quedan
    # en NULL para jobs completados con éxito o generados antes de las
    # migraciones que las agregan (``error_summary`` ->
    # ``t14msp19_job_error_summary``; ``error_type``/``error_reason`` ->
    # ``t14msp19_job_err_class``, agregada después/encadenada detrás por
    # el merge de ``fix/batch-rollback``).
    error_type = Column(String, nullable=True)
    error_reason = Column(String, nullable=True)
    error_summary = Column(Text, nullable=True)
    # Motivo del fallo del rollback (raw, del device o del verify) --
    # complementario a ``rollback_success=False``. NULL para jobs sin
    # rollback fallido o generados antes de la migración
    # u15msp20_job_rollback_error.
    rollback_error = Column(Text, nullable=True)
    current_step = Column(String, nullable=True)
    group_job_id = Column(String, nullable=True, index=True)

    __table_args__ = (
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )


class RefreshTokenModel(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    username = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class LoginAttemptModel(Base):
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False, index=True)
    ip_address = Column(String, nullable=False, index=True)
    attempted_at = Column(DateTime(timezone=True), nullable=False, index=True)
    succeeded = Column(Boolean, nullable=False, default=False)


class AuditLogModel(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    user = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    resource = Column(String, nullable=False, index=True)
    resource_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="success")
    details = Column(JSON, nullable=False, default=dict)
    # Frase legible de qué se hizo (o por qué falló) -- AuditRecord.summary,
    # ver docstring del campo y de _resumir() en models/audit.py. None para
    # eventos sin un RecursoGestionable detrás (auth/users/sites/...).
    summary = Column(Text, nullable=True)
    job_id = Column(String, nullable=True)
    device = Column(String, nullable=True)
    request_id = Column(String, nullable=True)
    parent_audit_id = Column(Integer, ForeignKey("audit_logs.id"), nullable=True, index=True)


class DeviceGroupModel(Base):
    __tablename__ = "device_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Marks the Site's Default group. Immutable per D7: cannot be renamed,
    # deleted, or demoted while the Site exists.
    is_default = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    # foreign_keys disambiguates from the reverse SiteModel.default_group_id
    # FK (two FK paths connect the tables).
    site = relationship(
        "SiteModel",
        back_populates="device_groups",
        foreign_keys=[site_id],
    )

    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_device_group_site_name"),
    )


class SiteModel(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(String, nullable=True)
    # MSP: Phase 1 — 'REGULAR' | 'BASE_INFRASTRUCTURE'. Phase 4 adds a partial
    # unique index enforcing exactly one BASE_INFRASTRUCTURE row.
    kind = Column(
        String(32), nullable=False, default="REGULAR", server_default="REGULAR"
    )
    # MSP: Phase 4 (M3) — stays nullable at the DB level despite the plan
    # calling for NOT NULL. The cyclic FK (sites↔device_groups) requires a
    # two-step INSERT (site first with default_group_id=NULL, then default
    # group with its site_id, then back-ref); no portable trick makes that
    # two-step land under a NOT NULL constraint. Enforced instead at the
    # ``site_service.create_site`` layer, which populates the column inside
    # the same transaction. Acknowledged deviation — see CHANGELOG Phase 4.
    default_group_id = Column(
        Integer,
        ForeignKey(
            "device_groups.id", use_alter=True, name="fk_sites_default_group_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    device_groups = relationship(
        "DeviceGroupModel",
        back_populates="site",
        foreign_keys="DeviceGroupModel.site_id",
    )
    # Direct relationship to the Default group. ``post_update=True`` breaks
    # the cyclic FK at flush time.
    default_group = relationship(
        "DeviceGroupModel",
        foreign_keys=[default_group_id],
        post_update=True,
        uselist=False,
    )

    __table_args__ = (UniqueConstraint("name", name="uq_site_name"),)


class RoleAssignmentModel(Base):
    """MSP: Phase 1 — per-scope grant.

    Sole source of per-scope authorization — replaces the pre-MSP global
    user role and site-scoping M2M.
    A user may hold multiple grants; ``VisibilityScope.rol_para(site, group)``
    resolves the effective role at request time as the **max** of the
    site-wide and group-specific grants (grants only elevate).

    ``device_group_id IS NULL`` → site-wide grant covering every current and
    future group in the site. ``device_group_id`` set → group-specific grant
    that can *raise* the role on that group above the site-wide baseline,
    but never lower it. See docs/USER_PERMISSIONS_UX_REDESIGN.md §2.1 for
    the semantic change from the earlier "most-specific-wins" rule.
    """

    __tablename__ = "role_assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    site_id = Column(
        Integer,
        ForeignKey("sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    device_group_id = Column(
        Integer,
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role = Column(String(32), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    user = relationship("UserModel", foreign_keys=[user_id])
    created_by = relationship("UserModel", foreign_keys=[created_by_user_id])
    site = relationship("SiteModel")
    device_group = relationship("DeviceGroupModel")

    __table_args__ = (
        CheckConstraint(
            "role IN ('observer', 'operator', 'admin')",
            name="ck_role_assignments_role",
        ),
        UniqueConstraint(
            "user_id",
            "site_id",
            "device_group_id",
            name="uq_role_assignments_scope",
        ),
    )
