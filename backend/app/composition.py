"""Raíz de composición — arma el grafo de dependencias del proceso una sola
vez al arrancar. FINAL_ARCHITECTURE.md §1.6.

No existía ningún lugar central antes de esta fase — main.py conectaba
FastAPI directo contra cada servicio. Este archivo va a ir creciendo en
fases futuras (Repository[X] concretos, Orquestador, GroupOperationRunner,
etc.) — no hace falta anticipar esas partes acá, cada fase agrega lo suyo.
"""

from __future__ import annotations

from app.core.repository import Repository
from app.db.models import DeviceVlanModel, DevicePortModel, DeviceSVIModel, DeviceGlobalConfigModel, DeviceArpMacModel, DeviceLogsModel
from app.repositories.audit_repository import AuditRepository
from app.repositories.device_group_repository import DeviceGroupRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.job_repository import JobRepository
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.role_assignment_repository import RoleAssignmentRepository
from app.repositories.site_repository import SiteRepository
from app.repositories.user_repository import UserRepository
from app.services.audit_listener import AuditListener
from app.services.cleanup_scheduler import CleanupScheduler
from app.services.dashboard_service import DashboardService
from app.services.device_sync_service import DeviceSyncService
from app.services.event_dispatcher import EventDispatcher
from app.services.group_operation_runner import GroupOperationRunner
from app.services.inventory import Inventory
from app.services.job_queue import JobQueue
from app.services.orquestador import Orquestador
from app.services.plugin_registry import PluginRegistry
from app.services.redis_coordinator import RedisCoordinator
from app.services.secret_vault import vault as secret_vault  # noqa: F401


def build_plugin_registry() -> PluginRegistry:
    from app.core.config import EXECUTION_MODE

    registry = PluginRegistry()
    if EXECUTION_MODE == "mock":
        from app.services.vendors.mock import MockVendor
        registry.registrar("huawei_vrp", MockVendor())
        registry.registrar("cisco_ios", MockVendor())
    else:
        from app.services.vendors.huawei.driver import HuaweiVendor
        from app.services.vendors.cisco.driver import CiscoVendor
        registry.registrar("huawei_vrp", HuaweiVendor())
        registry.registrar("cisco_ios", CiscoVendor())
    return registry


plugin_registry = build_plugin_registry()

redis_coordinator = RedisCoordinator()


def _vlan_to_orm(v: "VLAN"):
    return DeviceVlanModel(vlan_id=v.vlan_id, name=v.name, device=v.device)


def _vlan_to_domain(row) -> "VLAN":
    from app.models.vlan import VLAN
    return VLAN(vlan_id=row.vlan_id, name=row.name, device=row.device)


vlan_repository = Repository(
    DeviceVlanModel, _vlan_to_domain, _vlan_to_orm, pk_field=("vlan_id", "device"),
)


def _puerto_to_orm(p: "Puerto"):
    return DevicePortModel(
        interface=p.interface, device=p.device, description=p.description,
        admin_up=p.admin_up, mode=p.mode, access_vlan=p.access_vlan,
        allowed_vlans=p.allowed_vlans, poe_enabled=p.poe_enabled,
        storm_control_enabled=p.storm_control_enabled,
        storm_control_threshold=p.storm_control_threshold,
        storm_control_action=p.storm_control_action,
        storm_control_trap=p.storm_control_trap,
        operational_up=p.operational_up, speed=p.speed, duplex=p.duplex,
    )


def _puerto_to_domain(row) -> "Puerto":
    from app.models.port import Puerto
    return Puerto(
        interface=row.interface, device=row.device, description=row.description,
        admin_up=row.admin_up, mode=row.mode, access_vlan=row.access_vlan,
        allowed_vlans=row.allowed_vlans, poe_enabled=row.poe_enabled,
        storm_control_enabled=row.storm_control_enabled,
        storm_control_threshold=row.storm_control_threshold,
        storm_control_action=row.storm_control_action,
        storm_control_trap=row.storm_control_trap,
        operational_up=row.operational_up, speed=row.speed, duplex=row.duplex,
    )


puerto_repository = Repository(
    DevicePortModel, _puerto_to_domain, _puerto_to_orm, pk_field=("interface", "device"),
)


def _svi_to_orm(i: "SVI"):
    return DeviceSVIModel(
        vlan_id=i.vlan_id, device=i.device, description=i.description,
        admin_up=i.admin_up, ipv4_address=i.ipv4_address,
        ipv4_address_secondary=i.ipv4_address_secondary, ipv6_address=i.ipv6_address,
        acl_in=i.acl_in, acl_out=i.acl_out, dhcp_relay_servers=i.dhcp_relay_servers,
        operational_up=i.operational_up,
    )


def _svi_to_domain(row) -> "SVI":
    from app.models.svi import SVI
    return SVI(
        vlan_id=row.vlan_id, device=row.device, description=row.description,
        admin_up=row.admin_up, ipv4_address=row.ipv4_address,
        ipv4_address_secondary=row.ipv4_address_secondary, ipv6_address=row.ipv6_address,
        acl_in=row.acl_in, acl_out=row.acl_out, dhcp_relay_servers=row.dhcp_relay_servers,
        operational_up=row.operational_up,
    )


svi_repository = Repository(
    DeviceSVIModel, _svi_to_domain, _svi_to_orm,
    pk_field=("vlan_id", "device"),
)


def _global_config_to_orm(g: "GlobalConfig"):
    return DeviceGlobalConfigModel(
        device=g.device, hostname=g.hostname, running_config=g.running_config,
        device_version=g.device_version,
        snmp_enabled=g.snmp_enabled, snmp_version=g.snmp_version,
        snmp_community=g.snmp_community, snmp_permission=g.snmp_permission,
        snmp_trap_hosts=g.snmp_trap_hosts,
        ntp_servers=g.ntp_servers, dns_servers=g.dns_servers,
        log_servers=g.log_servers, log_level=g.log_level,
        routes=g.routes, acls=g.acls,
    )


def _global_config_to_domain(row) -> "GlobalConfig":
    from app.models.global_config import GlobalConfig
    return GlobalConfig(
        device=row.device, hostname=row.hostname, running_config=row.running_config,
        device_version=row.device_version,
        snmp_enabled=row.snmp_enabled, snmp_version=row.snmp_version,
        snmp_community=row.snmp_community, snmp_permission=row.snmp_permission,
        snmp_trap_hosts=row.snmp_trap_hosts,
        ntp_servers=row.ntp_servers, dns_servers=row.dns_servers,
        log_servers=row.log_servers, log_level=row.log_level,
        routes=row.routes, acls=row.acls,
    )


global_config_repository = Repository(
    DeviceGlobalConfigModel, _global_config_to_domain, _global_config_to_orm,
    pk_field="device",
)


def _arp_mac_to_orm(a: "ArpMacTables"):
    return DeviceArpMacModel(device=a.device, arp_table=a.arp_table, mac_table=a.mac_table)


def _arp_mac_to_domain(row) -> "ArpMacTables":
    from app.models.arp_mac import ArpMacTables
    return ArpMacTables(device=row.device, arp_table=row.arp_table, mac_table=row.mac_table)


arp_mac_repository = Repository(
    DeviceArpMacModel, _arp_mac_to_domain, _arp_mac_to_orm,
    pk_field="device",
)


def _device_logs_to_orm(l: "DeviceLogs"):
    return DeviceLogsModel(device=l.device, log_output=l.log_output)


def _device_logs_to_domain(row) -> "DeviceLogs":
    from app.models.device_logs import DeviceLogs
    return DeviceLogs(device=row.device, log_output=row.log_output)


device_logs_repository = Repository(
    DeviceLogsModel, _device_logs_to_domain, _device_logs_to_orm,
    pk_field="device",
)

job_repository = JobRepository()

role_assignment_repository = RoleAssignmentRepository()

device_repository = DeviceRepository()

device_group_repository = DeviceGroupRepository()

site_repository = SiteRepository()

user_repository = UserRepository()

audit_repository = AuditRepository()

login_attempt_repository = LoginAttemptRepository()


def get_role_assignment_repo() -> RoleAssignmentRepository:
    """FastAPI dependency shim over the module-level singleton — lets
    tests override authorization via ``app.dependency_overrides`` without
    reaching for EXECUTION_MODE or a fresh DB.
    """
    return role_assignment_repository


event_dispatcher = EventDispatcher()
event_dispatcher.suscribir(AuditListener(audit_repository))

orquestador = Orquestador(
    device_repository,
    {
        "vlan": vlan_repository, "puerto": puerto_repository, "svi": svi_repository,
        "global_config": global_config_repository,
    },
    job_repository, event_dispatcher, redis_coordinator,
)

group_operation_runner = GroupOperationRunner(orquestador, JobQueue(), job_repository)

cleanup_scheduler = CleanupScheduler(login_attempt_repository)

inventory = Inventory(
    device_repository, site_repository, device_group_repository,
    role_assignment_repository, job_repository, event_dispatcher, secret_vault,
)

device_sync_service = DeviceSyncService(
    vlan_repository, puerto_repository, svi_repository, global_config_repository, redis_coordinator,
    arp_mac_repository, device_logs_repository,
)

dashboard_service = DashboardService(redis_coordinator)
