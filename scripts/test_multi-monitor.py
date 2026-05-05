#!/usr/bin/env python

import requests
import time

BASE_URL = "http://127.0.0.1:8000/api/v1"
TOKEN = "TokenSession"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

jobs = []

def create_jobs():
    print("🚀 Creating jobs...\n")

    for i in range(1, 7):
        payload = {
            "vlan_id": 600 + i,
            "name": f"MULTI{i}",
            "devices": ["cisco1", "cisco2"]
        }

        r = requests.post(f"{BASE_URL}/vlans/", json=payload, headers=HEADERS)
        data = r.json()

        for job in data["jobs"]:
            jobs.append({
                "job_id": job["job_id"],
                "device": job["device"]
            })

    print(f"✅ Created {len(jobs)} jobs\n")


def monitor_jobs():
    print("📡 Monitoring jobs...\n")

    finished = set()

    while len(finished) < len(jobs):
        print("----- STATUS -----")

        for job in jobs:
            job_id = job["job_id"]

            if job_id in finished:
                continue

            r = requests.get(f"{BASE_URL}/jobs/{job_id}", headers=HEADERS)
            data = r.json()["data"]

            status = data["status"]

            print(f"{job['device']} | {job_id[:8]} | {status}")

            if status in ["completed", "failed"]:
                finished.add(job_id)

        print("\n")
        time.sleep(2)

    print("🏁 All jobs finished!")


if __name__ == "__main__":
    create_jobs()
    monitor_jobs()
