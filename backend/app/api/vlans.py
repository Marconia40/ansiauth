from fastapi import APIRouter, BackgroundTasks, HTTPException
from app.schemas.vlan import VLANCreate
from app.validators import vlan_validator
from app.services import job_service, vlan_service

router = APIRouter()

def _run_vlan_job(job_id: str, vlan: VLANCreate):
    job_service.update_job(job_id, "running")
    result = vlan_service.create_vlan(vlan)
    if result["rc"] != 0:
        job_service.update_job(job_id, "failed", {"error": result["stderr"]})
    else:
        job_service.update_job(job_id, "completed", {"output": result["stdout"]})

@router.post("/")
def create_vlan(vlan: VLANCreate, background_tasks: BackgroundTasks):
    try:
        vlan_validator.validate_vlan_id_range(vlan.vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
        vlan_validator.validate_vlan_name(vlan.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = job_service.create_job()
    background_tasks.add_task(_run_vlan_job, job.job_id, vlan)
    return {"job_id": job.job_id, "status": job.status}
