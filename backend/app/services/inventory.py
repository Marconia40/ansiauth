"""Inventory — Facade real con comportamiento, único punto de entrada para
Device CRUD/listado/movimiento. Fase 6, A5 — reescritura completa.

El cap real de 5 métodos se respeta -- el código real ya lo dice explícito
("Do NOT add non-Device operations here — service growth is capped at the
five methods below by the phase-3 acceptance criteria") y es una decisión
ya tomada, de una fase MSP anterior — no se reabre acá.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException

from app.core.exceptions import ValidationError
from app.db.models import DeviceGroupModel, DeviceModel, SiteModel
from app.db.session import get_session
from app.models.device import Device
from app.models.domain_event import DomainEvent

if TYPE_CHECKING:
    from app.models.visibility_scope import VisibilityScope

logger = logging.getLogger(__name__)


class Inventory:
    """Sole entry point for Device CRUD, listing, and movement."""

    def __init__(self, devices, sites, device_groups, role_assignments, jobs, auditor, vault):
        self._devices = devices              # DeviceRepository, Fase 3
        self._sites = sites                  # SiteRepository, A3
        self._device_groups = device_groups  # DeviceGroupRepository, A4
        self._role_assignments = role_assignments  # RoleAssignmentRepository, Fase 2
        self._jobs = jobs                    # JobRepository, Fase 4
        self._auditor = auditor              # EventDispatcher, Fase 3
        self._vault = vault                  # SecretVault, Fase 1

    # ─── Read ────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[Device]:
        return self._devices.get(name)

    def list(
        self,
        scope: "VisibilityScope",
        *,
        site_id: Optional[int] = None,
        device_group_id: Optional[int] = None,
    ) -> list[Device]:
        """Return devices visible to the caller, optionally filtered by site
        or group. Corrección real de esta fase (no solo forma): la versión
        vieja (``_visible_device_names``) tenía su propia copia del JOIN
        Device↔DeviceGroup -- reemplazada acá por
        ``DeviceRepository.nombres_visibles(scope)`` (Fase 3), que ya
        centraliza esa misma consulta para AuditRepository/JobRepository."""
        nombres = self._devices.nombres_visibles(scope)
        if nombres is None:
            devices = self._devices.list()
        elif not nombres:
            return []
        else:
            devices = [d for d in self._devices.list() if d.name in nombres]
        if site_id is not None:
            devices = [d for d in devices if d.site_id == site_id]
        if device_group_id is not None:
            devices = [d for d in devices if d.device_group_id == device_group_id]
        return devices

    # ─── Write ───────────────────────────────────────────────────────────

    def register(
        self,
        *,
        name: str,
        host: str,
        vendor: str,
        platform: str,
        username: str,
        password: str,
        site_id: int,
        device_group_id: Optional[int],
        actor: dict,
    ) -> Device:
        """Create a new device attached to ``device_group_id`` (or the site's
        Default group when ``device_group_id`` is None). El chequeo de vendor
        vive en ``Device.nuevo()`` (A5 nota), no acá."""
        site = self._sites.get(site_id)
        if site is None:
            raise ValidationError(f"site {site_id} no existe")
        if self._devices.get(name) is not None:
            # Corrección real encontrada portando create_device(): el real
            # rechaza con IntegrityError -> "already exists"; Repository[Device]
            # .add() es upsert (merge por pk_field="name"), así que sin este
            # chequeo explícito, registrar un nombre ya existente pisaba en
            # silencio el device viejo en vez de rechazar.
            raise ValidationError(f"Device '{name}' already exists")
        group = (
            self._device_groups.get(device_group_id)
            if device_group_id
            else self._sites.grupo_default(site)
        )
        if group is None:
            raise ValidationError(f"Device group {device_group_id} not found")
        if group.site_id != site_id:
            raise ValidationError(
                f"Device group {group.id} belongs to site {group.site_id}, not site {site_id}"
            )
        encrypted = self._vault.encrypt(password)
        device = Device.nuevo(name, host, vendor, platform, username, encrypted, group.id)
        # Corrección real encontrada comparando contra DevicePublic (schema
        # real, site_id/site_name/device_group_name NO son Optional): el
        # snippet canónico de esta fase auditaba/devolvía el `device` recién
        # construido con Device.nuevo(), que nunca tiene site_id/site_name/
        # device_group_name seteados (Device.nuevo() no los recibe) -- capturar
        # el retorno de add() (que sí los resuelve vía la relación ORM
        # device_group.site) antes de auditar/devolver, o _to_public() rompe
        # con un 500 de validación Pydantic en cada alta.
        device = self._devices.add(device)
        self._auditor.despachar([DomainEvent(
            "device_registrado", device, device, actor["username"], {"site_id": site_id},
        )])
        # Cache-first read model: disparo el sync inicial (VLAN + ports) en el
        # worker Celery. Fire-and-forget -- el POST vuelve al toque con el
        # device creado, y la UI muestra "sincronizando..." hasta que
        # devices.{vlans,ports}_synced_at se poblen. Si Redis/broker no está
        # disponible el .delay() puede tirar; en ese caso el alta ya se
        # persistió, así que sólo logueamos y dejamos que el usuario haga
        # refresh manual (paso 4) cuando el broker vuelva.
        try:
            from app.tasks import sync_device_task
            sync_device_task.delay(device.name, "all")
        except Exception as exc:
            logger.warning(
                "Inventory.register: sync task enqueue failed for %s (%s) — "
                "device registered without initial sync; refresh manually",
                device.name, exc,
            )
        logger.info("Inventory.register: %s → group=%s site=%s", name, group.id, site_id)
        return device

    def move(
        self,
        name: str,
        target_group_id: Optional[int],
        actor: dict,
        *,
        reason: Optional[str] = None,
        enforce_authz: bool = True,
    ) -> Device:
        """Move a device to a new group.

        Semantics
        ---------
        * ``target_group_id`` es un int → mueve el device a ese grupo (mismo
          site o cross-site — ``require_scope("move_device")`` en el router
          ya autorizó el rol correcto antes de llegar acá).
        * ``target_group_id`` es None → D8: mueve al Default del site
          **actual** del device.

        ``enforce_authz`` -- presente por paridad con el código real
        (``device_group_service.delete_group()``'s auto-move lo pasa en
        ``False``), pero **inerte** tanto acá como en el código real de hoy:
        ningún branch de este método lo lee. La autorización real corre
        siempre en el router (``require_scope``), antes de llamar a este
        método -- no duplicado.

        Guards
        ------
        * 404 si el device no existe.
        * 409 (D_active_job, vía ``JobRepository.activo_para``) si el device
          tiene un Job no-terminal -- salvo que el move sea un no-op (mismo
          orden que el real: un no-op nunca se bloquea por un job en curso).
        """
        with get_session() as session:
            row = session.query(DeviceModel).filter_by(name=name).first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Device '{name}' not found")

            source_group = row.device_group
            source_group_id = source_group.id if source_group is not None else None
            source_site_id = source_group.site_id if source_group is not None else None
            if source_site_id is None:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Device '{name}' is not attached to a group; refusing "
                        "to move — data-integrity invariant violated"
                    ),
                )

            if target_group_id is None:
                default_group_id = (
                    session.query(SiteModel.default_group_id)
                    .filter(SiteModel.id == source_site_id)
                    .scalar()
                )
                if default_group_id is None:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Site {source_site_id} has no Default group",
                    )
                resolved_group_id = int(default_group_id)
            else:
                resolved_group_id = int(target_group_id)

            target_group = (
                session.query(DeviceGroupModel).filter_by(id=resolved_group_id).first()
            )
            if target_group is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Target device group {resolved_group_id} does not exist",
                )
            if target_group.site_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Target device group {resolved_group_id} is not attached to a site"
                    ),
                )
            target_site_id = int(target_group.site_id)

            noop = source_group_id == resolved_group_id
            if not noop:
                if self._jobs.activo_para(name):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Cannot move device '{name}': a job is currently pending/running. "
                            "Wait for it to finish or cancel it."
                        ),
                    )
                row.device_group_id = resolved_group_id
                session.flush()

        device = self._devices.get(name)
        self._auditor.despachar([DomainEvent(
            "device_movido", device, device, actor["username"],
            {
                "name": name,
                "from_group_id": source_group_id,
                "from_site_id": source_site_id,
                "to_group_id": resolved_group_id,
                "to_site_id": target_site_id,
                "cross_site": source_site_id != target_site_id,
                "reason": reason,
                "resolved_from_default": target_group_id is None,
                "noop": noop,
            },
        )])
        logger.info(
            "Inventory.move: %s → group=%s (site=%s, cross_site=%s, reason=%s, noop=%s)",
            name, resolved_group_id, target_site_id,
            source_site_id != target_site_id, reason, noop,
        )
        return device

    def deregister(self, name: str, actor: dict) -> None:
        """Delete a device. Rejects the delete if a Job is still in flight."""
        device = self._devices.get(name)
        if device is None:
            raise HTTPException(status_code=404, detail=f"Device '{name}' not found")
        if self._jobs.activo_para(name):
            raise HTTPException(
                status_code=409,
                detail=f"Cannot delete device '{name}': a job is currently pending/running.",
            )
        with get_session() as session:
            session.query(DeviceModel).filter_by(name=name).delete()
        self._auditor.despachar([DomainEvent(
            "device_dado_de_baja", device, device, actor["username"], {"name": name},
        )])
        logger.info("Inventory.deregister: %s", name)
