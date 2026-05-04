import logging
import threading
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends

from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.schemas.vlan import VLANCreate, VLANUpdate
from app.services import audit_service, device_service, job_service, vlan_service
from app.validators import vlan_validator

logger = logging.getLogger(__name__)
router = APIRouter()


def _run_device_create_job(job_id: str, vlan_id: int, name: str, device: str, audit_id: str):
    job_service.update_job(job_id, "running")
    logger.info("Job %s: creating VLAN %s on device=%s", job_id, vlan_id, device)
    try:
        result = vlan_service.create_vlan_on_device(vlan_id, name, device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})
        audit_service.update_audit_status(audit_id, "completed")
        logger.info("Job %s: VLAN %s created on device=%s", job_id, vlan_id, device)
    except Exception as e:
        logger.error("Job %s (device=%s) failed: %s", job_id, device, str(e))
        job_service.update_job(job_id, "failed", error=str(e))
        audit_service.update_audit_status(audit_id, "failed")


def _run_delete_job(job_id: str, vlan_id: int, device: str, request_id: str, audit_id: str):
    job_service.update_job(job_id, "running")
    logger.info("Job %s: deleting VLAN %s on device=%s", job_id, vlan_id, device)
    try:
        result = vlan_service.delete_vlan(vlan_id, device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])

        try:
            remaining = vlan_service.get_vlans(device)
        except (ValueError, RuntimeError) as e:
            logger.warning("Job %s: could not verify VLAN deletion: %s", job_id, str(e))
            remaining = None

        if remaining is not None and any(v["vlan_id"] == vlan_id for v in remaining):
            logger.error("Job %s: VLAN %s still present after deletion on %s", job_id, vlan_id, device)
            job_service.update_job(job_id, "failed", error="VLAN still present after deletion")
            audit_service.update_audit_status(audit_id, "failed")
        else:
            job_service.update_job(job_id, "completed", {"output": result["stdout"]})
            audit_service.update_audit_status(audit_id, "completed")
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, str(e))
        job_service.update_job(job_id, "failed", error=str(e))
        audit_service.update_audit_status(audit_id, "failed")


def _run_update_job(job_id: str, vlan_id: int, data: VLANUpdate, audit_id: str):
    job_service.update_job(job_id, "running")
    logger.info("Job %s: updating VLAN %s on device=%s", job_id, vlan_id, data.device)
    try:
        result = vlan_service.update_vlan_description(vlan_id, data.description, data.device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})
        audit_service.update_audit_status(audit_id, "completed")
    except Exception as e:
        logger.error("Job %s failed: %s", job_id, str(e))
        job_service.update_job(job_id, "failed", error=str(e))
        audit_service.update_audit_status(audit_id, "failed")


@router.get("/")
def get_vlans(
    device: str | None = None,
    current_user: dict = Depends(require_role("observer")),
):
    try:
        data = vlan_service.get_vlans(device)
    except (ValueError, RuntimeError) as e:
        raise NotFoundError(str(e))
    return {"success": True, "data": data}


@router.post("/")
def create_vlan(
    vlan: VLANCreate,
    current_user: dict = Depends(require_role("operator")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan.vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
        vlan_validator.validate_vlan_name(vlan.name)
    except ValueError as e:
        raise ValidationError(str(e))

    for dev_name in vlan.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")

    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in vlan.devices:
        job = job_service.create_job(
            playbook="create_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan.vlan_id, "name": vlan.name},
        )
        audit = audit_service.log_action(
            user=current_user["username"],
            action="create_vlan",
            resource="vlan",
            details={"vlan_id": vlan.vlan_id, "name": vlan.name},
            status="pending",
            job_id=job.job_id,
            device=dev_name,
            request_id=request_id,
        )
        threading.Thread(
            target=_run_device_create_job,
            args=(job.job_id, vlan.vlan_id, vlan.name, dev_name, audit.id),
            daemon=True,
        ).start()
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})

    return {"success": True, "jobs": job_entries}


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

    try:
        existing_vlans = vlan_service.get_vlans(device)
    except (ValueError, RuntimeError) as e:
        raise NotFoundError(str(e))

    request_id = str(uuid.uuid4())

    if not any(v["vlan_id"] == vlan_id for v in existing_vlans):
        audit_service.log_action(
            user=current_user["username"],
            action="delete_vlan",
            resource="vlan",
            status="failure",
            details={"vlan_id": vlan_id, "device": device, "error": "VLAN does not exist"},
            device=device,
            request_id=request_id,
        )
        raise NotFoundError(f"VLAN {vlan_id} does not exist on device '{device}'")

    job = job_service.create_job(
        playbook="delete_vlan.yml",
        device=device,
        parameters={"vlan_id": vlan_id},
    )
    audit = audit_service.log_action(
        user=current_user["username"],
        action="delete_vlan",
        resource="vlan",
        details={"vlan_id": vlan_id, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )
    background_tasks.add_task(_run_delete_job, job.job_id, vlan_id, device, request_id, audit.id)
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

    request_id = str(uuid.uuid4())
    job = job_service.create_job(
        playbook="update_vlan.yml",
        device=data.device,
        parameters={"vlan_id": vlan_id, "description": data.description},
    )
    audit = audit_service.log_action(
        user=current_user["username"],
        action="update_vlan",
        resource="vlan",
        details={"vlan_id": vlan_id, "description": data.description, "device": data.device},
        status="pending",
        job_id=job.job_id,
        device=data.device,
        request_id=request_id,
    )
    background_tasks.add_task(_run_update_job, job.job_id, vlan_id, data, audit.id)
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}
