from fastapi import FastAPI
from app.api import vlans, jobs

app = FastAPI()

app.include_router(vlans.router, prefix="/api/v1/vlans", tags=["vlans"])
app.include_router(jobs.router, prefix="/api/v1/jobs", tags=["jobs"])

@app.get("/")
def read_root():
    return {"message": "Network Automation API running"}
