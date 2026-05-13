import requests
import time
import threading

BASE_URL = "http://127.0.0.1:8000"
TOKEN = "TokenSession"

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def create_vlan(vlan_id):
    payload = {
        "vlan_id": vlan_id,
        "name": f"TEST_{vlan_id}",
        "devices": ["cisco1"]
    }

    r = requests.post(f"{BASE_URL}/api/v1/vlans/", json=payload, headers=HEADERS)
    data = r.json()

    job_id = data["jobs"][0]["job_id"]
    print(f"[CREATE] VLAN {vlan_id} → job {job_id}")

    monitor_job(job_id)


def monitor_job(job_id):
    while True:
        r = requests.get(f"{BASE_URL}/api/v1/jobs/{job_id}", headers=HEADERS)
        data = r.json()["data"]

        print(f"[JOB {job_id}] status={data['status']} retry={data['retry_count']} rollback={data['rollback_performed']}")

        if data["status"] in ["completed", "failed"]:
            print(f"[FINAL] {job_id} → {data['status']}\n")
            break

        time.sleep(1)


def run_concurrent_test():
    threads = []

    for i in range(2):
        t = threading.Thread(target=create_vlan, args=(100+i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()


def run_rate_limit_test():
    threads = []

    for i in range(6):
        t = threading.Thread(target=create_vlan, args=(200+i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()


if __name__ == "__main__":
    print("=== CONCURRENCY TEST ===")
    run_concurrent_test()

    print("\n=== RATE LIMIT TEST ===")
    run_rate_limit_test()
