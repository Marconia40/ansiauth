# test_invalid_vlan.py
import requests

BASE_URL = "http://127.0.0.1:8000"
TOKEN = "TokenSession"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

payload = {
    "vlan_id": 601,  # inválido1
    "name": "INVALID",
    "devices": ["cisco1"]
}

r = requests.post(f"{BASE_URL}/api/v1/vlans/", json=payload, headers=headers)
print(r.json())
