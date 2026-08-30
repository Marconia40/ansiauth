"""DeviceRepository — first Repository[Device] instance in the migration.

Fase 1 cleaned up the Device dataclass but did not stand up its
repository. This class fills that gap and adds a single scoped read
(``nombres_visibles``) that AuditRepository.query() and (later)
JobRepository.query() need to answer "which device names can this
caller see" without recomputing grants each time.

device_service.py stayed live and untouched when this class was added
(Fase 3) — additive at the time. Fase 6 rewired every real caller
(``Inventory``, ``api/devices.py``); device_service.py has no live
caller left as of that fase.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.repository import Repository
from app.db.models import DeviceGroupModel, DeviceModel
from app.db.session import get_session
from app.models.device import Device
from app.models.visibility_scope import VisibilityScope


def _to_domain(row: DeviceModel) -> Device:
    """Project a persisted device row onto the domain model.

    Kept 1:1 with device_service._to_domain — the mapping did not change
    across Fases 1/2. Site attributes are derived through
    ``device.device_group.site``, the only site pointer devices carry
    after Phase 5.
    """
    group = getattr(row, "device_group", None)
    group_id = group.id if group is not None else None
    group_name = group.name if group is not None else None
    site_id = group.site_id if group is not None else None
    site_name = group.site.name if group is not None and group.site is not None else None
    return Device(
        name=row.name,
        host=row.host,
        vendor=row.vendor,
        platform=row.platform or "ios",
        username=row.username,
        encrypted_password=row.encrypted_password,
        id=str(row.id),
        created_at=row.created_at or datetime.now(timezone.utc),
        site_id=site_id,
        site_name=site_name,
        device_group_id=group_id,
        device_group_name=group_name,
    )


def _to_orm(d: Device) -> DeviceModel:
    return DeviceModel(
        name=d.name,
        host=d.host,
        vendor=d.vendor,
        platform=d.platform,
        username=d.username,
        encrypted_password=d.encrypted_password,
        device_group_id=d.device_group_id,
    )


class DeviceRepository(Repository):
    def __init__(self):
        # Device uses .name as its identity — DeviceModel.id (autoincrement)
        # and Device.id (dataclass, uuid-derived) are incompatible types,
        # so .id is not a valid upsert key. See FASE_1.md, Repository[T]
        # composite-PK note.
        super().__init__(DeviceModel, _to_domain, _to_orm, pk_field="name")

    def nombres_visibles(self, scope: VisibilityScope) -> "set[str] | None":
        """Set of device names the caller can see under MSP rules.

        ``None`` = system-admin (caller skips the IN filter entirely).
        Empty set = authenticated but with zero grants.

        Mirrors inventory_service._visible_device_names, but takes a
        pre-resolved VisibilityScope instead of recomputing grants from
        role_assignments per call.
        """
        if scope.es_system_admin:
            return None
        if not scope.site_ids and not scope.device_group_ids:
            return set()
        from sqlalchemy import or_

        with get_session() as session:
            q = session.query(DeviceModel.name).join(
                DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id,
            )
            conds = []
            if scope.site_ids:
                conds.append(DeviceGroupModel.site_id.in_(scope.site_ids))
            if scope.device_group_ids:
                conds.append(DeviceModel.device_group_id.in_(scope.device_group_ids))
            return {r[0] for r in q.filter(or_(*conds)).all()}
