# Network Automation API

## Description

This project implements a REST API for multi-vendor network device automation.
The system abstracts vendor-specific configuration through a unified interface and uses Ansible as the automation engine.

It supports real execution against Cisco devices (tested with GNS3 + Cisco IOSv) and a mock mode for development and testing without real hardware.

---

## Architecture

```
User → API (FastAPI) → Validations → Job (async) → Service → (Mock / Ansible) → Result
```

Main components:

| Component | Responsibility |
|---|---|
| **API (FastAPI)** | Exposes REST endpoints |
| **Auth (JWT + RBAC)** | Token-based authentication with role enforcement |
| **Validators** | Input validation logic |
| **Job System** | Async task execution with real-time status tracking |
| **Services** | Business logic (VLAN, devices, audit, secrets, etc.) |
| **Ansible** | Real execution on network devices via ansible-runner |
| **Audit Logger** | Per-device, per-job action tracing with DB persistence |
| **Rate Limiter** | Sliding-window per-device job throttling |
| **Device Locks** | Per-device concurrency serialization |

---

## Requirements

* Python 3.10+
* pip
* virtualenv (recommended)
* ansible-runner
* A `.env` file with `SECRET_KEY`

---

## Installation

### 1. Clone the repository

```bash
git clone <repo_url>
cd ansiauth
```

### 2. Create virtual environment

```bash
python -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Install Ansible collections

```bash
ansible-galaxy collection install cisco.ios
ansible-galaxy collection install ansible.netcommon
```

### 5. Configure environment variables

Create a `.env` file in the project root:

```env
SECRET_KEY=your-secret-key-here
EXECUTION_MODE=mock   # or "real" for live Ansible execution
DATABASE_URL=sqlite:///./app.db
```

---

## Running the API

```bash
cd backend/
uvicorn app.main:app --reload
```

Or from the project root:

```bash
PYTHONPATH=backend uvicorn app.main:app --reload
```

API available at:

```
http://127.0.0.1:8000
```

Swagger UI (interactive docs):

```
http://127.0.0.1:8000/docs
```

---

## Authentication & Authorization

The API uses **JWT tokens** with short-lived expiration.

### Login

```
POST /api/v1/auth/login
```

```bash
curl -X POST /api/v1/auth/login \
  -d "username=admin&password=admin123"
```

Returns a bearer token to use in subsequent requests.

### Roles

| Role | Permissions |
|---|---|
| `admin` | Full access (devices, VLANs, audit log) |
| `operator` | Execute configuration changes (VLAN operations) |
| `observer` | Read-only access |

### Swagger usage

Click the **Authorize** button in Swagger UI, enter `Bearer <token>`, and all requests will be authenticated automatically.

---

## VLAN Management

### Supported operations

| Method | Endpoint | Role | Description |
|---|---|---|---|
| `POST` | `/api/v1/vlans/` | operator | Create VLAN |
| `GET` | `/api/v1/vlans/` | observer | List VLANs |
| `DELETE` | `/api/v1/vlans/{vlan_id}` | admin | Delete VLAN |
| `PATCH` | `/api/v1/vlans/{vlan_id}` | operator | Update VLAN name |

### Validation rules

* VLAN ID must be between **2–4094**
* Reserved VLANs are rejected: `1, 1002, 1003, 1004, 1005`
* Name: no spaces, maximum 32 characters

### Example — Create VLAN (single device)

```json
POST /api/v1/vlans/
{
  "vlan_id": 100,
  "name": "MGMT",
  "devices": ["cisco1"]
}
```

### VLAN Idempotency

Before any create or update, the system captures the current VLAN state from the device (`show vlan brief`).

**CREATE behavior:**

| Scenario | Result |
|---|---|
| VLAN does not exist | Create proceeds normally |
| VLAN exists, same name | Job completes as no-op (`vlan_already_exists_no_op`) |
| VLAN exists, different name | Job fails immediately (caller must delete first) |

**UPDATE behavior:**

| Scenario | Result |
|---|---|
| VLAN exists | Update proceeds |
| VLAN does not exist | Job fails immediately (`vlan_not_found`) |

This guarantees the system is **safe to run multiple times** without unintended side effects.

---

## Multi-Device Support

A single request can target multiple devices simultaneously. The system creates **one independent job per device** and executes them in parallel.

### Example

```json
POST /api/v1/vlans/
{
  "vlan_id": 100,
  "name": "TEST",
  "devices": ["cisco1", "cisco2"]
}
```

**Response:**

```json
{
  "success": true,
  "jobs": [
    { "device": "cisco1", "job_id": "abc-111", "status": "pending" },
    { "device": "cisco2", "job_id": "abc-222", "status": "pending" }
  ]
}
```

* Each job runs independently — one failure does not block others
* Each job gets its own audit log entry
* All jobs from the same request share a `request_id` for traceability

---

## Job System

All operations are executed asynchronously in the background.

### Job lifecycle

```
pending → running → completed
                 → failed
                 → cancelled
```

### Job fields

| Field | Description |
|---|---|
| `job_id` | Unique identifier |
| `status` | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `current_step` | Real-time execution step: `retrying`, `completed`, `failed` |
| `retry_count` | Number of retry attempts performed so far |
| `max_retries` | Configured maximum retries (default: 3) |
| `last_error` | Most recent error message (updated on each retry) |
| `pre_state` | VLAN state captured before execution (`existed`, `vlan_data`) |
| `rollback_performed` | Whether a rollback was successfully executed |
| `error` | Final error message if the job failed |
| `result` | Output from the Ansible playbook on success |
| `created_at` | UTC timestamp of job creation |
| `started_at` | UTC timestamp when execution began |
| `finished_at` | UTC timestamp of completion |

### Check job status (real-time)

```
GET /api/v1/jobs/{job_id}
```

During active retries, `retry_count`, `last_error`, and `current_step` are updated in real time, so polling this endpoint shows live progress.

### Cancel a job

```
POST /api/v1/jobs/{job_id}/cancel
```

### List all jobs

```
GET /api/v1/jobs/
```

---

## Reliability & Production Safety

### Concurrency Control

Each device has an exclusive threading lock. Jobs targeting the same device are serialized — no two jobs run against the same device simultaneously. Jobs for different devices run in parallel.

### Rate Limiting

A sliding-window rate limiter enforces a maximum of **5 jobs per device per 60-second window**. Jobs that exceed this limit block until a slot becomes available.

### Smart Retry with Error Classification

When an Ansible playbook fails, the system classifies the error as **transient** or **permanent**:

**Transient errors** (trigger retry with exponential backoff):
* SSH connection refused
* Connection timeout / timed out
* Unable to connect
* SSH failure / SSH error
* Network unreachable / UNREACHABLE
* No route to host

**Permanent errors** (fail immediately, no retry):
* Configuration syntax errors
* Permission denied
* Any other unclassified error

Retry strategy:
* Maximum **3 retries**
* Exponential backoff: `1s → 2s → 4s`
* Error text checked in both `stderr` **and** `stdout` (Ansible places SSH errors in stdout)

During retries, `retry_count`, `last_error`, and `current_step="retrying"` are updated on the job in real time so the caller can observe progress without waiting for final completion.

### Safe Rollback

The system captures device state **before every operation** (`show vlan brief`). If an operation fails, rollback is attempted based on that pre-state:

| Operation | Rollback action |
|---|---|
| `create_vlan` fails, VLAN did not exist before | Run `no vlan X` to clean up any partial state |
| `update_vlan` fails | Restore original VLAN name |
| `delete_vlan` fails, VLAN existed before | Recreate VLAN with original name |

Rollback is **best-effort** — network devices are not transactional. Rollback success is tracked separately in `rollback_performed`.

Both the job and the audit record include `pre_state` and `rollback_performed` for full observability.

---

## Device Management

Devices must be registered before being targeted by VLAN operations.

| Method | Endpoint | Role | Description |
|---|---|---|---|
| `POST` | `/api/v1/devices/` | admin | Add device |
| `GET` | `/api/v1/devices/` | admin | List devices |
| `GET` | `/api/v1/devices/{name}` | admin | Get device |
| `DELETE` | `/api/v1/devices/{name}` | admin | Remove device |

### Example — Register a device

```json
POST /api/v1/devices/
{
  "name": "cisco1",
  "host": "10.10.10.1",
  "vendor": "cisco_ios",
  "username": "admin",
  "password": "cisco123"
}
```

---

## Secret Management

Device passwords are **never stored in plaintext**.

* Passwords are encrypted using the `SECRET_KEY` environment variable before being saved to the database
* Decryption happens at runtime, only when a playbook is about to run
* The `SECRET_KEY` must be set before starting the API — losing it makes stored passwords unrecoverable
* Encryption uses symmetric cryptography (Fernet)

---

## Audit Logging

Every significant action is logged to the database with full traceability.

### Audit log entry fields

| Field | Description |
|---|---|
| `user` | Who performed the action |
| `action` | What was done (e.g. `create_vlan`, `login`) |
| `resource` | What was affected (`vlan`, `job`, `auth`, etc.) |
| `device` | Specific device targeted |
| `job_id` | Linked background job |
| `request_id` | Groups all entries from the same API request |
| `status` | `pending`, `completed`, `failed` |
| `details` | Additional context (JSON) — includes `pre_state`, `rollback_performed`, `retries`, `error_type`, `duration_seconds` |
| `timestamp` | UTC datetime |

For multi-device operations, **one audit entry is created per device**, each with its own `job_id`. All entries from the same request share the same `request_id`.

### Details field structure (on failure)

```json
{
  "retries": 3,
  "rollback_performed": true,
  "duration_seconds": 7.42,
  "pre_state": { "existed": false, "vlan_data": null },
  "error": {
    "type": "ansible_error",
    "rc": 1,
    "stderr": "SSH connection refused",
    "error_type": "transient"
  },
  "error_type": "transient"
}
```

### Query the audit log (admin only)

```
GET /api/v1/audit/
GET /api/v1/audit/?user=operator
GET /api/v1/audit/?action=create_vlan
GET /api/v1/audit/?resource=vlan&limit=50&skip=0
```

---

## Ansible Integration

Playbooks are located in `backend/ansible/project/`. ansible-runner is used to execute them with per-device dynamic inventories.

Each playbook call receives:
* A dynamically built inventory string with device credentials
* Extra vars (`vlan_id`, `vlan_name`, `device`)

**Each concurrent ansible-runner call uses an isolated temporary inventory file** to prevent race conditions when targeting multiple devices in parallel. The inventory format is:

```ini
[all]
cisco1 ansible_host=10.10.10.1 ansible_user=admin ansible_password=cisco123 ansible_network_os=ios ansible_connection=network_cli
```

### Execution Flow

1. API receives request
2. One job and one audit entry are created per device (synchronously, before any execution starts)
3. Background threads are dispatched per device
4. Rate limiter waits for a slot on the target device
5. Per-device lock is acquired
6. Pre-state is captured from the device
7. Idempotency check runs (real mode only)
8. Ansible playbook executes with exponential backoff retry
9. On failure, rollback is attempted using the captured pre-state
10. Job and audit are updated with final status, `pre_state`, `rollback_performed`, `retry_count`

---

## Real Network Integration (GNS3 + Cisco IOSv)

The API has been tested against Cisco IOSv devices running in GNS3.

### SSH configuration

Cisco IOSv uses legacy SSH algorithms. Add this to `~/.ssh/config`:

```
Host 10.10.10.*
    KexAlgorithms +diffie-hellman-group14-sha1,diffie-hellman-group1-sha1
    HostKeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa
```

This resolves `no matching key exchange method` and `no matching host key type` errors that occur with modern OpenSSH clients connecting to older Cisco IOS images.

---

## Testing

### Run all tests

```bash
cd backend/
python -m pytest tests/ -v
```

Tests run in mock mode by default — no real devices required.

### Test coverage

| Test file | What it covers |
|---|---|
| `test_auth.py` | JWT login, RBAC enforcement per role |
| `test_vlans.py` | VLAN CRUD, validation, error codes |
| `test_jobs.py` | Job lifecycle, status transitions |
| `test_audit.py` | Audit persistence, filtering, multi-device rows |
| `test_devices.py` | Device registration and management |
| `test_multi_device_vlan.py` | Parallel execution, per-device jobs, inventory isolation |
| `test_reliability.py` | Concurrency locks, rate limiting, rollback, audit fields |
| `test_lifecycle.py` | Job/audit lifecycle consistency, structured errors, stuck-state recovery |
| `test_vlan_semantics.py` | Idempotency: create existing (same/different name), update non-existent |
| `test_smart_retry.py` | Transient vs permanent error classification, retry count, real-time visibility |
| `test_rollback.py` | Create/update/delete rollback, `pre_state` on job and audit |

Total: **108 tests**, all passing.

---

## Current Project Status

| Feature | Status |
|---|---|
| VLAN create / delete / update | ✅ |
| Input validation | ✅ |
| Async job system | ✅ |
| JWT authentication | ✅ |
| Role-based access control (RBAC) | ✅ |
| Device management (DB-backed) | ✅ |
| Encrypted password storage | ✅ |
| Multi-device parallel execution | ✅ |
| Audit logging (per device + job) | ✅ |
| Request grouping via `request_id` | ✅ |
| Real Ansible execution | ✅ |
| GNS3 / Cisco IOSv integration | ✅ |
| Automated test suite (pytest) | ✅ |
| Per-device concurrency locking | ✅ |
| Sliding-window rate limiting | ✅ |
| Smart retry (transient vs permanent) | ✅ |
| Exponential backoff | ✅ |
| Real-time retry visibility in job API | ✅ |
| VLAN idempotency (pre-state check) | ✅ |
| Safe rollback with pre-state | ✅ |
| `pre_state` on job and audit | ✅ |
| Structured error in audit (`error_type`) | ✅ |
| Dynamic isolated Ansible inventory | ✅ |
| Job stuck-state recovery (`ensure_final_state`) | ✅ |

---

## Limitations

* Cisco IOSv requires legacy SSH crypto configuration (see SSH section above)
* `SECRET_KEY` rotation is not currently supported — changing the key invalidates all stored device passwords
* Job store is in-memory; jobs are lost on service restart (audit log persists in the DB)
* Batch Ansible execution (single run across multiple devices) is not supported — each device gets its own playbook call

## Future Improvements

* WebSocket / SSE support for real-time job status push (currently requires polling)
* Database migration support (Alembic)
* Support for additional vendors (Huawei, Juniper)
* Pagination and filtering on the job list endpoint
* `SECRET_KEY` rotation utility
* Persistent job store (move from in-memory to database)
* Batch Ansible execution across device groups

---

## Team Usage

To replicate the environment:

1. Clone the repository
2. Create and activate a virtual environment
3. Install dependencies (`pip install -r requirements.txt`)
4. Install Ansible collections (`ansible-galaxy collection install cisco.ios ansible.netcommon`)
5. Create `.env` with `SECRET_KEY` and `EXECUTION_MODE`
6. Run `uvicorn app.main:app --reload` from `backend/`
7. Test from Swagger at `http://127.0.0.1:8000/docs`

---

## Notes

* No real devices are required for development — mock mode simulates all operations
* The system is designed to scale to real execution without any API changes
* Audit logs are persisted to the database and survive service restarts
* All passwords are encrypted at rest and decrypted only at playbook execution time
* Pre-state capture and rollback work in both mock and real execution modes
* Error classification checks both `stderr` and `stdout` — Ansible places SSH errors in stdout
