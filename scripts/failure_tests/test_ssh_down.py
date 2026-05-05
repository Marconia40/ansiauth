# test_ssh_down.py
import requests
import time

BASE_URL = "http://127.0.0.1:8000"
TOKEN = "TokenSession"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

payload = {
    "vlan_id": 701,
    "name": "SSH_DOWN",
    "devices": ["cisco1"]
}

print("🚨 Disable SSH on device BEFORE running this")

r = requests.post(f"{BASE_URL}/api/v1/vlans/", json=payload, headers=headers)
job_id = r.json()["jobs"][0]["job_id"]

while True:
    time.sleep(2)
    res = requests.get(f"{BASE_URL}/api/v1/jobs/{job_id}", headers=headers).json()
    print(res["data"])
    if res["data"]["status"] in ["completed", "failed"]:
        break
