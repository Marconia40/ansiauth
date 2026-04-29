from app.models.job import Job

_jobs: dict[str, Job] = {}

def create_job() -> Job:
    job = Job()
    _jobs[job.job_id] = job
    return job

def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)

def update_job(job_id: str, status: str, result: dict | None = None):
    job = _jobs.get(job_id)
    if job:
        job.status = status
        job.result = result
