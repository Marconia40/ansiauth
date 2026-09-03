from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated, require_device
from app.models.interfaz_virtual import InterfazVirtual
from app.models.visibility_scope import VisibilityScope
from app.schemas.device_sync import SyncedResource
from app.schemas.interfaz_virtual import (
    InterfazVirtualAclClearRequest,
    InterfazVirtualAclUpdateRequest,
    InterfazVirtualAdminStateUpdateRequest,
    InterfazVirtualCreateRequest,
    InterfazVirtualDeleteRequest,
    InterfazVirtualDescriptionClearRequest,
    InterfazVirtualDescriptionUpdateRequest,
    InterfazVirtualDhcpRelayAddRequest,
    InterfazVirtualDhcpRelayRemoveRequest,
    InterfazVirtualIpv4ClearRequest,
    InterfazVirtualIpv4UpdateRequest,
    InterfazVirtualIpv6ClearRequest,
    InterfazVirtualIpv6UpdateRequest,
    InterfazVirtualRead,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _authz_device(
    scope: VisibilityScope, device_name: str, *, min_role: str, device: "Device | None" = None,
) -> None:
    """Mismo criterio que ``api/ports.py``'s ``_authz_device`` -- delega en
    ``authorize_device()``, pasando ``(site_id, device_group_id)`` ya
    resueltos cuando el caller ya tiene el ``Device`` en mano."""
    resolved = (device.site_id, device.device_group_id) if device is not None else None
    authorize_device(scope, device_name, "interfaz_virtual_device_op", min_role, resolved=resolved)


def _require_driver_with(device: "Device", method_name: str):
    """Mismo criterio que ``api/ports.py``'s ``_require_port_driver_with``
    -- 501 ``VENDOR_NOT_SUPPORTED`` si el driver concreto no sobreescribe
    *method_name* (todavía stub en ``VendorDriver``)."""
    from app.services.vendors.base import VendorDriver

    driver = device.driver
    if getattr(type(driver), method_name) is getattr(VendorDriver, method_name):
        logger.info(
            "Interfaz virtual %s unimplemented on driver=%s for device=%s",
            method_name, type(driver).__name__, device.name,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "This virtual interface operation is not yet supported for this vendor.",
            },
        )
    return driver


def _check_device_not_locked(device_name: str) -> None:
    """Mismo criterio que ``api/ports.py``'s ``_check_device_not_locked``."""
    from app.composition import redis_coordinator

    if redis_coordinator.esta_ocupado(device_name):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "DEVICE_LOCKED",
                "message": (
                    f"Device '{device_name}' is busy with another operation — "
                    "retry shortly"
                ),
            },
        )


def _require_vlan_existente(device_name: str, vlan_id: int) -> None:
    """RF-INTERV-9: crear una SVI la asocia a su VLAN -- se valida acá que
    la VLAN ya exista en *device_name* antes de encolar la creación, contra
    la cache (``vlan_repository``, poblada por ``sync_device_task``), mismo
    dato que ya sirve ``GET /vlans`` -- no una lectura en vivo extra
    bloqueando el endpoint."""
    from app.composition import vlan_repository

    if vlan_repository.get((vlan_id, device_name)) is None:
        raise ValidationError(
            f"VLAN {vlan_id} does not exist on device '{device_name}' -- create it first "
            f"(or refresh the VLAN cache via POST /devices/{device_name}/vlans/refresh)"
        )


@router.get(
    "/",
    summary="List virtual interfaces",
    description=(
        "Retrieve the virtual interface (SVI) inventory of a single device. "
        "Cache-first — reads from the synced cache (populated on device "
        "registration, manual refresh, or after a write), not live from "
        "the equipment. Requires observer role or higher; site-scoped "
        "users may only query devices in their allowed sites."
    ),
)
def list_interfaces_virtuales(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, interfaz_virtual_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    interfaces = interfaz_virtual_repository.list(device=name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        "count": len(interfaces),
        "interfaces_virtuales": [
            InterfazVirtualRead(
                vlan_id=i.vlan_id, description=i.description, admin_up=i.admin_up,
                operational_up=i.operational_up, ipv4_address=i.ipv4_address,
                ipv4_address_secondary=i.ipv4_address_secondary,
                ipv6_address=i.ipv6_address, acl_in=i.acl_in, acl_out=i.acl_out,
                dhcp_relay_servers=i.dhcp_relay_servers,
            ).model_dump()
            for i in interfaces
        ],
    }
    synced_at, sync_error = device_sync_service.metadata(name, "interfaces_virtuales")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.post(
    "/",
    summary="Create virtual interface",
    description=(
        "Create the virtual interface (SVI) for a VLAN on a single device "
        "(RF-INTERV-01). The VLAN must already exist on the device "
        "(RF-INTERV-09 — associating the SVI to its VLAN is implicit in "
        "the identity: creating the interface for vlan_id=10 already "
        "associates it to VLAN 10). Accepts an optional `description`, "
        "applied atomically right after creation. If the interface already "
        "exists, the response reports it as a no-op duplicate instead of "
        "re-applying. Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry the frontend can poll via "
        "`GET /api/v1/jobs/{job_id}` and `GET /api/v1/group-jobs/{id}`. "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def create_interfaz_virtual(
    name: str,
    data: InterfazVirtualCreateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = InterfazVirtual(vlan_id=data.vlan_id, crear=True, description=data.description)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "create_interfaz_virtual")
    _require_vlan_existente(name, data.vlan_id)

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/",
    summary="Delete virtual interface",
    description=(
        "Delete the virtual interface (SVI) of a VLAN on a single device "
        "(RF-INTERV-02). No-op when the interface does not exist. Executed "
        "asynchronously: the response carries a `group_job_id` and "
        "per-device job entry. Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def delete_interfaz_virtual(
    name: str,
    data: InterfazVirtualDeleteRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, eliminar=True)
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "delete_interfaz_virtual")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/admin-state",
    summary="Set virtual interface admin state",
    description=(
        "Administratively enable or disable a virtual interface (SVI) "
        "(RF-INTERV-03). Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry. Requires operator role "
        "or higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def set_interfaz_admin_state(
    name: str,
    data: InterfazVirtualAdminStateUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, admin_up=data.enabled)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/description",
    summary="Set virtual interface description",
    description=(
        "Assign a description to a virtual interface (SVI) (RF-INTERV-08). "
        "To clear it, use `DELETE .../description` instead — a value is "
        "always required here. Executed asynchronously: the response "
        "carries a `group_job_id` and per-device job entry. Requires "
        "operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def set_interfaz_description(
    name: str,
    data: InterfazVirtualDescriptionUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, description=data.description)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_description")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/description",
    summary="Clear virtual interface description",
    description=(
        "Clear the description of a virtual interface (SVI) (RF-INTERV-08). "
        "Executed asynchronously: the response carries a `group_job_id` "
        "and per-device job entry. Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def clear_interfaz_description(
    name: str,
    data: InterfazVirtualDescriptionClearRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, description="")
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_description")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/ipv4",
    summary="Set virtual interface IPv4 address",
    description=(
        "Assign the IPv4 address of a virtual interface (SVI) (RF-INTERV-03). "
        "Address is given in CIDR notation (e.g. '10.10.10.11/24'). "
        "`secondary=true` targets the secondary IPv4 address instead of "
        "the primary — requires a primary already configured on the "
        "interface, checked against live device state when the job runs. "
        "To clear an address, use `DELETE .../ipv4` instead. Executed "
        "asynchronously: the response carries a `group_job_id` and "
        "per-device job entry. Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def set_interfaz_ipv4(
    name: str,
    data: InterfazVirtualIpv4UpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    campo = "ipv4_address_secondary" if data.secondary else "ipv4_address"
    entidad = InterfazVirtual(vlan_id=data.vlan_id, **{campo: data.ipv4_address})
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_ipv4_secondary" if data.secondary else "set_interfaz_ipv4")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/ipv4",
    summary="Clear virtual interface IPv4 address",
    description=(
        "Clear the IPv4 address of a virtual interface (SVI) (RF-INTERV-03). "
        "`secondary=true` clears the secondary IPv4 address instead of the "
        "primary. Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry. Requires operator role "
        "or higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def clear_interfaz_ipv4(
    name: str,
    data: InterfazVirtualIpv4ClearRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    campo = "ipv4_address_secondary" if data.secondary else "ipv4_address"
    entidad = InterfazVirtual(vlan_id=data.vlan_id, **{campo: ""})
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_ipv4_secondary" if data.secondary else "set_interfaz_ipv4")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/ipv6",
    summary="Set virtual interface IPv6 address",
    description=(
        "Assign the IPv6 address of a virtual interface (SVI) (RF-INTERV-04). "
        "Address is given in CIDR notation (e.g. '2001:db8::1/64'). "
        "Requires the applicable global IPv6 precondition on the device "
        "(Cisco: `ipv6 unicast-routing`; Huawei: `ipv6 enable`, applied "
        "per-interface by the driver). To clear it, use `DELETE .../ipv6` "
        "instead. Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry. Requires operator role "
        "or higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def set_interfaz_ipv6(
    name: str,
    data: InterfazVirtualIpv6UpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, ipv6_address=data.ipv6_address)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_ipv6")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/ipv6",
    summary="Clear virtual interface IPv6 address",
    description=(
        "Clear the IPv6 address of a virtual interface (SVI) (RF-INTERV-04). "
        "Executed asynchronously: the response carries a `group_job_id` "
        "and per-device job entry. Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def clear_interfaz_ipv6(
    name: str,
    data: InterfazVirtualIpv6ClearRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, ipv6_address="")
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_ipv6")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/acl",
    summary="Set virtual interface ACL binding",
    description=(
        "Bind an existing ACL to a virtual interface (SVI) in a given "
        "direction (RF-INTERV-04). Binds an ACL that already exists on "
        "the device — does not create it (that's RF-GLOBAL-04, out of "
        "scope here); binding a name/number the device doesn't recognize "
        "is rejected with a 400 before enqueueing. To clear a binding, use "
        "`DELETE .../acl` instead. Executed asynchronously: the response "
        "carries a `group_job_id` and per-device job entry. Requires "
        "operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def set_interfaz_acl(
    name: str,
    data: InterfazVirtualAclUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    campo = "acl_in" if data.direction == "in" else "acl_out"
    entidad = InterfazVirtual(vlan_id=data.vlan_id, **{campo: data.acl_name})
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    driver = _require_driver_with(dev, "set_interfaz_acl")
    _require_driver_with(dev, "list_acl_names")

    acls_existentes = driver.list_acl_names(dev, dev.password)
    if data.acl_name not in acls_existentes:
        raise ValidationError(
            f"ACL '{data.acl_name}' does not exist on device '{name}' -- "
            f"create it first (RF-GLOBAL-04, out of scope here)"
        )

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/acl",
    summary="Clear virtual interface ACL binding",
    description=(
        "Remove the ACL bound to a virtual interface (SVI) in a given "
        "direction (RF-INTERV-04). Executed asynchronously: the response "
        "carries a `group_job_id` and per-device job entry. Requires "
        "operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def clear_interfaz_acl(
    name: str,
    data: InterfazVirtualAclClearRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    campo = "acl_in" if data.direction == "in" else "acl_out"
    entidad = InterfazVirtual(vlan_id=data.vlan_id, **{campo: ""})
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_acl")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.post(
    "/dhcp-relay",
    summary="Add a virtual interface DHCP relay server",
    description=(
        "Add a single DHCP relay/helper-address server to a virtual "
        "interface (SVI) (RF-INTERV-05), incremental — leaves any other "
        "server already configured untouched. Rejected when the interface "
        "has no address configured for the server's IP family (IPv4/IPv6); "
        "a no-op when the server is already present. Executed "
        "asynchronously: the response carries a `group_job_id` and "
        "per-device job entry. Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def add_interfaz_dhcp_relay(
    name: str,
    data: InterfazVirtualDhcpRelayAddRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, dhcp_relay_add=data.server)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_dhcp_relay")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/dhcp-relay",
    summary="Remove a virtual interface DHCP relay server",
    description=(
        "Remove a single DHCP relay/helper-address server from a virtual "
        "interface (SVI) (RF-INTERV-05), incremental — leaves any other "
        "server already configured untouched. A no-op when the server is "
        "not present. Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry. Requires operator role "
        "or higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def remove_interfaz_dhcp_relay(
    name: str,
    data: InterfazVirtualDhcpRelayRemoveRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = InterfazVirtual(vlan_id=data.vlan_id, dhcp_relay_remove=data.server)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _check_device_not_locked(name)
    _require_driver_with(dev, "set_interfaz_dhcp_relay")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)
