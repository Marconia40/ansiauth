import logging

from fastapi import APIRouter, Body, Depends, Query

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import (
    obtener_scope,
    require_authenticated,
    require_scope,
    visible_or_404,
)
from app.models.visibility_scope import VisibilityScope
from app.schemas.device import DeviceCreate, DeviceMove, DevicePublic, DeviceUpdate

# Reused from global_config.py's own `/mac`/`/arp` `include` query param --
# same safe whitelist (no pipe/newline injection risk when embedded
# directly into a device CLI command, see CiscoVendor.search_mac_table()).
from app.api.global_config import _ARP_MAC_INCLUDE_RE

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_public(device) -> dict:
    return DevicePublic(
        id=device.id,
        name=device.name,
        host=device.host,
        vendor=device.vendor,
        platform=device.platform,
        username=device.username,
        auth_method=device.auth_method,
        site_id=device.site_id,
        site_name=device.site_name,
        device_group_id=device.device_group_id,
        device_group_name=device.device_group_name,
    ).model_dump()


@router.get(
    "/",
    summary="List devices",
    description="Return all registered network devices. Credentials are never included in responses. Requires observer role or higher.",
)
def list_devices(
    site_id: int | None = None,
    device_group_id: int | None = None,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    devices = inventory.list(scope, site_id=site_id, device_group_id=device_group_id)
    return ok([_to_public(d) for d in devices])


@router.get(
    "/{name}",
    summary="Get device",
    description="Return a single device by its unique name. Requires observer role or higher.",
)
def get_device(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    device = inventory.get(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    return ok(_to_public(device))


@router.post(
    "/",
    summary="Register device",
    description=(
        "Add a new network device to the inventory. "
        "The password is encrypted at rest using AES-256 and never returned in responses. "
        "Requires admin on the target site."
    ),
)
def create_device(
    data: DeviceCreate = Body(
        ...,
        # Named examples so Swagger's Example Value panel shows a picker --
        # a schema-level example doesn't render a dropdown, this does.
        openapi_examples={
            "password": {
                "summary": "Password auth",
                "value": {
                    "name": "switch-01",
                    "host": "192.168.1.10",
                    "vendor": "cisco_ios",
                    "platform": "ios",
                    "username": "admin",
                    "auth_method": "password",
                    "password": "s3cr3tpass",
                    "site_id": 1,
                    "device_group_id": 3,
                },
            },
            "key": {
                "summary": "SSH key auth",
                "value": {
                    "name": "switch-02",
                    "host": "192.168.1.11",
                    "vendor": "huawei_vrp",
                    "platform": "ce",
                    "username": "netconf",
                    "auth_method": "key",
                    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
                    "site_id": 1,
                    "device_group_id": 3,
                },
            },
        },
    ),
    current_user: dict = Depends(require_scope("register_device")),
):
    from app.composition import inventory

    try:
        device = inventory.register(
            name=data.name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
            auth_method=data.auth_method,
            private_key=data.private_key,
            site_id=data.site_id,
            device_group_id=data.device_group_id,
            actor=current_user,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    return ok(_to_public(device))


@router.put(
    "/{name}",
    summary="Update device",
    description=(
        "Update connection details for a registered device. "
        "All fields are optional — only provided fields are changed. "
        "Requires admin on the device."
    ),
)
def update_device(
    name: str,
    data: DeviceUpdate,
    current_user: dict = Depends(require_scope("edit_device")),
):
    from app.composition import device_repository, event_dispatcher
    from app.models.domain_event import DomainEvent
    from app.services.secret_vault import vault

    provided = data.model_dump(exclude_unset=True)
    if not provided:
        raise ValidationError("No fields provided for update")
    device = device_repository.get(name)
    if device is None:
        raise NotFoundError(f"Device '{name}' not found")
    try:
        device.actualizar(
            host=data.host, vendor=data.vendor, platform=data.platform,
            username=data.username, password=data.password,
            auth_method=data.auth_method, private_key=data.private_key,
            vault=vault,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    device = device_repository.add(device)
    audit_fields = {k: v for k, v in provided.items() if k not in ("password", "private_key")}
    # Antes llamaba audit_repository.append(AuditRecord(...)) directo --
    # único endpoint del ciclo de vida de Device que se saltaba
    # EventDispatcher (register()/move()/deregister() en Inventory ya
    # despachan DomainEvent) -- corrección real, FINAL_ARCHITECTURE.md §6:
    # si mañana se agrega un 2do EventListener, esta escritura ahora
    # participa igual que las otras 3.
    event_dispatcher.despachar([DomainEvent(
        "update_device", device, device, current_user["username"],
        {"name": name, "updated_fields": audit_fields},
    )])
    return ok(_to_public(device))


@router.post(
    "/{name}/move",
    summary="Move device to a group",
    description=(
        "Change a device's owning group. Same-site moves require operator on the "
        "device; cross-site moves require admin on both sides. Per D8, passing "
        "`device_group_id: null` moves the device to its current site's Default "
        "group (the device-level 'remove from group' action)."
    ),
    status_code=200,
)
def move_device(
    name: str,
    body: DeviceMove,
    current_user: dict = Depends(require_scope("move_device")),
):
    from app.composition import inventory

    device = inventory.move(name, body.device_group_id, actor=current_user)
    return ok(_to_public(device))



@router.post(
    "/{name}/vlans/refresh",
    status_code=202,
    summary="Refresh device VLAN cache",
    description=(
        "Trigger a background sync of *device*'s VLAN cache from the equipment. "
        "Returns immediately with the task id; the frontend polls "
        "`GET /api/v1/vlans?device={name}` and watches `synced_at` / "
        "`sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device — same criterion as "
        "reading the VLAN list."
    ),
)
def refresh_device_vlans(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "vlans")
    return ok({"device": name, "scope": "vlans", "task_id": result.id})


@router.post(
    "/{name}/ports/refresh",
    status_code=202,
    summary="Refresh device port cache",
    description=(
        "Trigger a background sync of *device*'s port cache from the equipment. "
        "Returns immediately with the task id; the frontend polls "
        "`GET /api/v1/ports?device={name}` and watches `synced_at` / "
        "`sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device — same criterion as "
        "reading the port list."
    ),
)
def refresh_device_ports(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "ports")
    return ok({"device": name, "scope": "ports", "task_id": result.id})


@router.post(
    "/{name}/svis/refresh",
    status_code=202,
    summary="Refresh device virtual interface cache",
    description=(
        "Trigger a background sync of *device*'s virtual interface (SVI) "
        "cache from the equipment. Returns immediately with the task id; "
        "the frontend polls `GET /api/v1/svis?device={name}` "
        "and watches `synced_at` / `sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device — same criterion as "
        "reading the virtual interface list."
    ),
)
def refresh_device_svis(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "svis")
    return ok({"device": name, "scope": "svis", "task_id": result.id})


@router.post(
    "/{name}/sync/refresh",
    status_code=202,
    summary="Refresh device VLAN + port + SVI caches together",
    description=(
        "Trigger a single background sync of *device*'s VLAN, port and SVI "
        "caches from the equipment (``scope=\"all\"``, i.e. ``sync_core()``) "
        "-- one device lock acquisition and one SSH read session (2 on "
        "Huawei) instead of the 2-3 separate connections that calling "
        "`/vlans/refresh` + `/ports/refresh` (+ `/svis/refresh`) "
        "individually would open. Returns immediately with the task id; "
        "the frontend polls the usual per-scope GETs and watches "
        "`synced_at` / `sync_in_progress` on each to detect completion. "
        "Requires observer role or higher on the device."
    ),
)
def refresh_device_sync(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "all")
    return ok({"device": name, "scope": "all", "task_id": result.id})


@router.post(
    "/{name}/global-config/refresh",
    status_code=202,
    summary="Refresh device global configuration cache",
    description=(
        "Trigger a background sync of *device*'s global configuration "
        "cache from the equipment (version, hostname, SNMP status, "
        "routing table, ACL names — RF-GLOBAL-01/02/03/04). Returns "
        "immediately with the task id; the frontend polls "
        "`GET /api/v1/devices/{name}/global-config` and watches "
        "`synced_at` / `sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device — same criterion "
        "as reading the global configuration."
    ),
)
def refresh_device_global_config(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "global_config")
    return ok({"device": name, "scope": "global_config", "task_id": result.id})


@router.post(
    "/{name}/global-config/arp-mac/refresh",
    status_code=202,
    summary="Refresh device ARP/MAC table cache",
    description=(
        "Trigger a background sync of *device*'s ARP and MAC address "
        "table cache from the equipment. Separate scope from "
        "`global-config/refresh` on purpose — ARP/MAC aren't needed for "
        "any write's no-op check and can be a lot of data, so they're not "
        "synced automatically on device registration or as part of a "
        "general refresh; this is the only way to (re)populate them. "
        "Returns immediately with the task id; the frontend polls "
        "`GET /api/v1/devices/{name}/global-config/arp` (or `/mac`) and "
        "watches `synced_at` / `sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device — same criterion "
        "as reading the tables."
    ),
)
def refresh_device_arp_mac(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "arp_mac")
    return ok({"device": name, "scope": "arp_mac", "task_id": result.id})


@router.post(
    "/{name}/global-config/mac/search",
    status_code=202,
    summary="Search the device's MAC address table live",
    description=(
        "Query the device directly for MAC entries matching `include` "
        "(`show mac address-table | include ...` / `display mac-address | "
        "include ...`), instead of relying on the cached full table. "
        "Exists because a device with a very large MAC table can fail to "
        "sync the full table at all (the interactive SSH read drops mid-"
        "stream — confirmed live against a large device) while a filtered, "
        "on-device search of the same table stays small enough to work. "
        "Not cached, not part of `GET .../global-config/mac` — a one-off "
        "live query, tracked like any other async operation: returns a "
        "`job_id` immediately, poll `GET /api/v1/jobs/{job_id}` and read "
        "`result.entries` once `status == \"completed\"`. Requires "
        "observer role or higher on the device."
    ),
)
def search_device_mac_table(
    name: str,
    include: str = Query(..., pattern=_ARP_MAC_INCLUDE_RE),
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    import uuid

    from app.composition import inventory, job_repository
    from app.models.job import Job
    from app.tasks import search_mac_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")

    job = Job(
        operation="mac_search",
        device=name,
        parameters={"pattern": include},
        parameters_summary=f"Search MAC table on {name} for '{include}'",
        group_job_id=str(uuid.uuid4()),
    )
    job_repository.add(job)
    search_mac_task.delay(name, include, job.job_id)
    return ok({"job_id": job.job_id, "group_job_id": job.group_job_id, "status": job.status})


@router.post(
    "/{name}/global-config/logs/refresh",
    status_code=202,
    summary="Refresh device log buffer cache",
    description=(
        "Trigger a background sync of *device*'s local log buffer "
        "(`show logging`/`display logbuffer`) from the equipment. Same "
        "criterion as `arp-mac/refresh` — not needed for any write's "
        "no-op check and can be a lot of data, so it's not synced "
        "automatically on device registration or as part of a general "
        "refresh. Returns immediately with the task id; the frontend "
        "polls `GET /api/v1/devices/{name}/global-config/logs` and "
        "watches `synced_at` / `sync_in_progress` to detect completion. "
        "Requires observer role or higher on the device."
    ),
)
def refresh_device_logs(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory
    from app.tasks import sync_device_task

    if inventory.get(name) is None:
        raise NotFoundError(f"Device '{name}' not found")
    visible_or_404(scope, name, "device", "observer", f"Device '{name}' not found")
    result = sync_device_task.delay(name, "logs")
    return ok({"device": name, "scope": "logs", "task_id": result.id})


@router.delete(
    "/{name}",
    summary="Delete device",
    description="Permanently remove a device from the inventory. Requires admin on the device.",
)
def delete_device(
    name: str,
    current_user: dict = Depends(require_scope("delete_device")),
):
    from app.composition import inventory

    inventory.deregister(name, actor=current_user)
    return ok({"name": name})
