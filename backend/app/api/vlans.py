from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.core.dependencies import require_role
from app.schemas.vlan import VLANCreate, VLANUpdate
from app.services import job_service, vlan_service
from app.validators import vlan_validator

router = APIRouter()


def _run_create_job(job_id: str, vlan: VLANCreate):
    job_service.update_job(job_id, "running")
    result = vlan_service.create_vlan(vlan)
    if result["rc"] != 0:
        job_service.update_job(job_id, "failed", {"error": result["stderr"]})
    else:
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})


def _run_delete_job(job_id: str, vlan_id: int, device: str):
    job_service.update_job(job_id, "running")
    result = vlan_service.delete_vlan(vlan_id, device)
    if result["rc"] != 0:
        job_service.update_job(job_id, "failed", {"error": result["stderr"]})
    else:
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})


def _run_update_job(job_id: str, vlan_id: int, data: VLANUpdate):
    job_service.update_job(job_id, "running")
    result = vlan_service.update_vlan_description(vlan_id, data.description, data.device)
    if result["rc"] != 0:
        job_service.update_job(job_id, "failed", {"error": result["stderr"]})
    else:
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})


@router.get("/", dependencies=[Depends(require_role("observer"))])
def get_vlans():
    return vlan_service.get_vlans()


@router.post("/", dependencies=[Depends(require_role("operator"))])
def create_vlan(vlan: VLANCreate, background_tasks: BackgroundTasks):
    try:
        vlan_validator.validate_vlan_id_range(vlan.vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
        vlan_validator.validate_vlan_name(vlan.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = job_service.create_job()
    background_tasks.add_task(_run_create_job, job.job_id, vlan)
    return {"job_id": job.job_id, "status": job.status}


@router.delete("/{vlan_id}", dependencies=[Depends(require_role("admin"))])
def delete_vlan(vlan_id: int, device: str, background_tasks: BackgroundTasks):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = job_service.create_job()
    background_tasks.add_task(_run_delete_job, job.job_id, vlan_id, device)
    return {"job_id": job.job_id, "status": job.status}


@router.patch("/{vlan_id}", dependencies=[Depends(require_role("operator"))])
def update_vlan(vlan_id: int, data: VLANUpdate, background_tasks: BackgroundTasks):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_description(data.description)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = job_service.create_job()
    background_tasks.add_task(_run_update_job, job.job_id, vlan_id, data)
    return {"job_id": job.job_id, "status": job.status}
