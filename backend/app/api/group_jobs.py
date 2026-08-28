import logging

from fastapi import APIRouter, Depends

from app.composition import job_repository
from app.core.exceptions import NotFoundError
from app.core.scope import require_authenticated

logger = logging.getLogger(__name__)
router = APIRouter()


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
    current_user: dict = Depends(require_authenticated),
):
    resumen = job_repository.resumen_de_grupo(group_job_id)
    if resumen is None:
        raise NotFoundError(f"Group job '{group_job_id}' not found")
    return {"success": True, "data": resumen}
