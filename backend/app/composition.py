"""Raíz de composición — arma el grafo de dependencias del proceso una sola
vez al arrancar. FINAL_ARCHITECTURE.md §1.6.

No existía ningún lugar central antes de esta fase — main.py conectaba
FastAPI directo contra cada servicio. Este archivo va a ir creciendo en
fases futuras (Repository[X] concretos, Orquestador, GroupOperationRunner,
etc.) — no hace falta anticipar esas partes acá, cada fase agrega lo suyo.
"""

from __future__ import annotations

from app.core.repository import Repository
from app.db.models import DeviceVlanModel, DevicePortModel
from app.repositories.audit_repository import AuditRepository
from app.repositories.device_group_repository import DeviceGroupRepository
from app.repositories.device_repository import DeviceRepository
from app.repositories.job_repository import JobRepository
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.role_assignment_repository import RoleAssignmentRepository
from app.services.audit_listener import AuditListener
from app.services.cleanup_scheduler import CleanupScheduler
from app.services.event_dispatcher import EventDispatcher
from app.services.group_operation_runner import GroupOperationRunner
from app.services.job_queue import JobQueue
from app.services.orquestador import Orquestador
from app.services.plugin_registry import PluginRegistry
from app.services.redis_coordinator import RedisCoordinator
from app.services.secret_service import vault as secret_vault  # noqa: F401


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
    )


def _puerto_to_domain(row) -> "Puerto":
    from app.models.port import Puerto
    return Puerto(
        interface=row.interface, device=row.device, description=row.description,
        admin_up=row.admin_up, mode=row.mode, access_vlan=row.access_vlan,
        allowed_vlans=row.allowed_vlans, poe_enabled=row.poe_enabled,
    )


puerto_repository = Repository(
    DevicePortModel, _puerto_to_domain, _puerto_to_orm, pk_field=("interface", "device"),
)

job_repository = JobRepository()

role_assignment_repository = RoleAssignmentRepository()

device_repository = DeviceRepository()

device_group_repository = DeviceGroupRepository()

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
    device_repository, {"vlan": vlan_repository, "puerto": puerto_repository},
    job_repository, event_dispatcher, redis_coordinator,
)

group_operation_runner = GroupOperationRunner(orquestador, JobQueue(), job_repository)

cleanup_scheduler = CleanupScheduler(login_attempt_repository)
