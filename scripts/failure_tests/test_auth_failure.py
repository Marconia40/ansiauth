# test_auth_failure.py
import requests
import time

BASE_URL = "http://127.0.0.1:8000"
TOKEN = "TokenSession"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

payload = {
    "vlan_id": 700,
    "name": "AUTH_FAIL",
    "devices": ["cisco3"]
}

print("🚨 Changing device password to incorrect one (manually before running)")

r = requests.post(f"{BASE_URL}/api/v1/vlans/", json=payload, headers=headers)
data = r.json()

job_id = data["jobs"][0]["job_id"]

while True:
    time.sleep(2)
    status = requests.get(f"{BASE_URL}/api/v1/jobs/{job_id}", headers=headers).json()
    print(status["data"])
    if status["data"]["status"] in ["completed", "failed"]:
        break
