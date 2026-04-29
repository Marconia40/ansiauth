from fastapi import FastAPI

from app.api import audit, auth, jobs, vlans

app = FastAPI()

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(vlans.router, prefix="/api/v1/vlans", tags=["vlans"])
app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"])
app.include_router(audit.router, prefix="/api/v1/audit", tags=["audit"])


@app.get("/")
def read_root():
    return {"message": "Network Automation API running"}
