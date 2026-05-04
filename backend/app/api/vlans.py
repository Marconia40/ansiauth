import logging
import threading
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.schemas.vlan import VLANCreate, VLANDelete, VLANUpdate
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


def _run_delete_job(job_id: str, vlan_id: int, device: str, audit_id: str):
    job_service.update_job(job_id, "running")
    logger.info("Job %s: deleting VLAN %s on device=%s", job_id, vlan_id, device)
    try:
        # Verify VLAN exists before attempting deletion
        try:
            existing = vlan_service.get_vlans(device)
            if not any(v["vlan_id"] == vlan_id for v in existing):
                raise DeviceExecutionError(f"VLAN {vlan_id} does not exist on device '{device}'")
        except DeviceExecutionError:
            raise
        except Exception as e:
            logger.warning("Job %s: could not verify VLAN pre-existence: %s", job_id, str(e))

        result = vlan_service.delete_vlan(vlan_id, device)
        if result["rc"] != 0:
            raise DeviceExecutionError(result["stderr"])

        try:
            remaining = vlan_service.get_vlans(device)
        except Exception as e:
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


def _run_update_job(job_id: str, vlan_id: int, description: str, device: str, audit_id: str):
    job_service.update_job(job_id, "running")
    logger.info("Job %s: updating VLAN %s on device=%s", job_id, vlan_id, device)
    try:
        result = vlan_service.update_vlan_description(vlan_id, description, device)
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
    devices: list[str] | None = Query(default=None),
    current_user: dict = Depends(require_role("observer")),
):
    if devices:
        result = {}
        for dev in devices:
            try:
                result[dev] = vlan_service.get_vlans(dev)
            except (ValueError, RuntimeError) as e:
                raise NotFoundError(str(e))
        return {"success": True, "data": result}
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
    data: VLANDelete,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("admin")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
    except ValueError as e:
        raise ValidationError(str(e))

    for dev_name in data.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")

    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in data.devices:
        job = job_service.create_job(
            playbook="delete_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id},
        )
        audit = audit_service.log_action(
            user=current_user["username"],
            action="delete_vlan",
            resource="vlan",
            details={"vlan_id": vlan_id, "device": dev_name},
            status="pending",
            job_id=job.job_id,
            device=dev_name,
            request_id=request_id,
        )
        background_tasks.add_task(_run_delete_job, job.job_id, vlan_id, dev_name, audit.id)
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})

    return {"success": True, "jobs": job_entries}


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

    for dev_name in data.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")

    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in data.devices:
        job = job_service.create_job(
            playbook="update_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id, "description": data.description},
        )
        audit = audit_service.log_action(
            user=current_user["username"],
            action="update_vlan",
            resource="vlan",
            details={"vlan_id": vlan_id, "description": data.description, "device": dev_name},
            status="pending",
            job_id=job.job_id,
            device=dev_name,
            request_id=request_id,
        )
        background_tasks.add_task(_run_update_job, job.job_id, vlan_id, data.description, dev_name, audit.id)
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})

    return {"success": True, "jobs": job_entries}
