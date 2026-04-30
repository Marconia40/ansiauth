import logging

from fastapi import APIRouter, BackgroundTasks, Depends

from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.schemas.vlan import VLANCreate, VLANUpdate
from app.services import audit_service, device_service, job_service, vlan_service
from app.validators import vlan_validator

logger = logging.getLogger(__name__)
router = APIRouter()


def _run_create_job(job_id: str, vlan: VLANCreate):
    job_service.update_job(job_id, "running")
    try:
        result = vlan_service.create_vlan(vlan)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, str(e))
        job_service.update_job(job_id, "failed", error=str(e))


def _run_delete_job(job_id: str, vlan_id: int, device: str):
    job_service.update_job(job_id, "running")
    try:
        result = vlan_service.delete_vlan(vlan_id, device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, str(e))
        job_service.update_job(job_id, "failed", error=str(e))


def _run_update_job(job_id: str, vlan_id: int, data: VLANUpdate):
    job_service.update_job(job_id, "running")
    try:
        result = vlan_service.update_vlan_description(vlan_id, data.description, data.device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, str(e))
        job_service.update_job(job_id, "failed", error=str(e))


@router.get("/")
def get_vlans(current_user: dict = Depends(require_role("observer"))):
    return {"success": True, "data": vlan_service.get_vlans()}


@router.post("/")
def create_vlan(
    vlan: VLANCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan.vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
        vlan_validator.validate_vlan_name(vlan.name)
    except ValueError as e:
        raise ValidationError(str(e))

    if not device_service.get_device(vlan.device):
        raise NotFoundError(f"Device '{vlan.device}' not found")

    job = job_service.create_job()
    background_tasks.add_task(_run_create_job, job.job_id, vlan)
    audit_service.log_action(
        user=current_user["username"],
        action="create_vlan",
        resource="vlan",
        details={"vlan_id": vlan.vlan_id, "name": vlan.name, "device": vlan.device},
        job_id=job.job_id,
    )
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}


@router.delete("/{vlan_id}")
def delete_vlan(
    vlan_id: int,
    device: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("admin")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
    except ValueError as e:
        raise ValidationError(str(e))

    if not device_service.get_device(device):
        raise NotFoundError(f"Device '{device}' not found")

    job = job_service.create_job()
    background_tasks.add_task(_run_delete_job, job.job_id, vlan_id, device)
    audit_service.log_action(
        user=current_user["username"],
        action="delete_vlan",
        resource="vlan",
        details={"vlan_id": vlan_id, "device": device},
        job_id=job.job_id,
    )
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}


@router.patch("/{vlan_id}")
def update_vlan(
    vlan_id: int,
    data: VLANUpdate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_description(data.description)
    except ValueError as e:
        raise ValidationError(str(e))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")

    job = job_service.create_job()
    background_tasks.add_task(_run_update_job, job.job_id, vlan_id, data)
    audit_service.log_action(
        user=current_user["username"],
        action="update_vlan",
        resource="vlan",
        details={"vlan_id": vlan_id, "description": data.description, "device": data.device},
        job_id=job.job_id,
    )
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}
