import logging

from fastapi import APIRouter, Depends

from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.services import group_job_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_group_job(gj) -> dict:
    return {
        "group_job_id": gj.group_job_id,
        "status": gj.status,
        "operation": gj.operation,
        "playbook": gj.playbook,
        "parameters": gj.parameters,
        "created_at": gj.created_at.isoformat() if gj.created_at else None,
        "started_at": gj.started_at.isoformat() if gj.started_at else None,
        "finished_at": gj.finished_at.isoformat() if gj.finished_at else None,
        "execution_summary": gj.execution_summary(),
        "device_results": [r.to_dict() for r in gj.device_results],
    }


@router.get(
    "/{group_job_id}",
    summary="Get group job",
    description=(
        "Return the aggregated status and per-device results of a group job. "
        "A group job represents one logical VLAN operation dispatched across N devices. "
        "Accessible to all authenticated users."
    ),
)
def get_group_job(
    group_job_id: str,
    current_user: dict = Depends(get_current_user),
):
    gj = group_job_service.get_group_job(group_job_id)
    if not gj:
        raise NotFoundError(f"Group job '{group_job_id}' not found")
    return {"success": True, "data": _format_group_job(gj)}
