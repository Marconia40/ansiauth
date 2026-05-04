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

* **API (FastAPI)**: exposes REST endpoints
* **Auth (JWT + RBAC)**: token-based authentication with role enforcement
* **Validators**: input validation logic
* **Job System**: async task execution with status tracking
* **Services**: business logic (VLAN, devices, audit, etc.)
* **Ansible**: real execution on network devices via ansible-runner
* **Audit Logger**: per-device, per-job action tracing with DB persistence

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

### 4. Configure environment variables

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

| Role       | Permissions                                      |
|------------|------------------------------------------        |
| `admin`    | Full access (devices, VLANs, audit log)          |
| `operator` | Execute configuration changes (VLAN operations)  |
| `observer` | Read-only access                                 |

### Swagger usage

Click the **Authorize** button in Swagger UI, enter `Bearer <token>`, and all requests will be authenticated automatically.

---

## VLAN Management

### Supported operations

| Method   | Endpoint                   | Role       | Description       |
|----------|----------------------------|------------|-------------------|
| `POST`   | `/api/v1/vlans/`           | operator   | Create VLAN       |
| `GET`    | `/api/v1/vlans/`           | observer   | List VLANs        |
| `DELETE` | `/api/v1/vlans/{vlan_id}`  | admin      | Delete VLAN       |
| `PATCH`  | `/api/v1/vlans/{vlan_id}`  | operator   | Update VLAN       |

### Validation rules

* VLAN ID must be between **2–4094**
* Reserved VLANs are rejected: `1, 1002, 1003, 1004, 1005`
* Name:
  * No spaces
  * Maximum 32 characters

### Example — Create VLAN (single device)

```json
POST /api/v1/vlans/
{
  "vlan_id": 100,
  "name": "MGMT",
  "devices": ["cisco1"]
}
```

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

### Check job status

```
GET /api/v1/jobs/{job_id}
```

### Cancel a job

```
POST /api/v1/jobs/{job_id}/cancel
```

### List all jobs

```
GET /api/v1/jobs/
```

---

## Device Management

Devices must be registered in the system before being targeted by VLAN operations.

| Method   | Endpoint                    | Role  | Description      |
|----------|-----------------------------|-------|------------------|
| `POST`   | `/api/v1/devices/`          | admin | Add device       |
| `GET`    | `/api/v1/devices/`          | admin | List devices     |
| `GET`    | `/api/v1/devices/{name}`    | admin | Get device       |
| `DELETE` | `/api/v1/devices/{name}`    | admin | Remove device    |

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
* SECRET_KEY rotation is not currently supported

---

## Audit Logging

Every significant action is logged to the database with full traceability.

### Audit log entry fields

| Field        | Description                                      |
|--------------|--------------------------------------------------|
| `user`       | Who performed the action                         |
| `action`     | What was done (e.g. `create_vlan`, `login`)      |
| `resource`   | What was affected (`vlan`, `job`, `auth`, etc.)  |
| `device`     | Specific device targeted                         |
| `job_id`     | Linked background job                            |
| `request_id` | Groups all entries from the same API request     |
| `status`     | `success` or `failure`                           |
| `details`    | Additional context (JSON)                        |
| `timestamp`  | UTC datetime                                     |

For multi-device operations, **one audit entry is created per device**, each with its own `job_id`. All entries from the same request share the same `request_id`.

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

**Important**: each concurrent ansible-runner call uses an isolated temporary directory for its inventory file to prevent race conditions when targeting multiple devices in parallel.

---

### Dynamic Inventory Strategy

Each job builds its own isolated inventory at runtime.

This ensures:
- No conflicts between parallel executions
- Correct device targeting
- No shared state between jobs

This design avoids common Ansible issues such as:
- "no hosts matched"
- race conditions when running concurrent playbooks


### Execution Flow

1. API receives request
2. A job is created per device
3. Credentials are decrypted
4. A dynamic inventory is generated
5. ansible-runner executes the playbook
6. Result is stored and exposed via job endpoint

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

### Error Handling

- If Ansible execution fails, the job is marked as `failed`
- Errors are captured and stored in the job result
- Audit logs reflect failure status per device
- Failures in one device do not affect others in multi-device operations

## Testing

### Run all tests

```bash
cd backend/
python -m pytest tests/ -v
```

Or from the project root:

```bash
PYTHONPATH=backend python -m pytest backend/tests -v
```

Tests run in mock mode by default (no real devices required).

### Test coverage

* Authentication and RBAC
* VLAN operations (create, delete, update, get)
* Input validation
* Job lifecycle
* Multi-device parallel execution
* Audit log persistence and filtering
* Device management
* Password encryption

---

## Current Project Status

| Feature                            | Status |
|------------------------------------|--------|
| VLAN create / delete / update      | ✅     |
| Input validation                   | ✅     |
| Async job system                   | ✅     |
| JWT authentication                 | ✅     |
| Role-based access control (RBAC)   | ✅     |
| Device management (DB-backed)      | ✅     |
| Encrypted password storage         | ✅     |
| Multi-device parallel execution    | ✅     |
| Audit logging (per device + job)   | ✅     |
| Request grouping via `request_id`  | ✅     |
| Real Ansible execution             | ✅     |
| GNS3 / Cisco IOSv integration      | ✅     |
| Automated test suite (pytest)      | ✅     |

---

## Limitations

- Cisco IOSv requires legacy SSH crypto support
- No retry mechanism implemented yet
- Batch execution is not yet supported (one job per device)

## Future Improvements

* Retry mechanism for failed jobs
* Batch execution (single Ansible run across multiple devices)
* Database migration support (Alembic)
* Support for additional vendors (Huawei, Juniper)
* Pagination and filtering on job list endpoint
* Websocket support for real-time job status updates

---

## Team Usage

To replicate the environment:

1. Clone the repository
2. Create and activate a virtual environment
3. Install dependencies (`pip install -r requirements.txt`)
4. Create `.env` with `SECRET_KEY` and `EXECUTION_MODE`
5. Run `uvicorn app.main:app --reload` from `backend/`
6. Test from Swagger at `http://127.0.0.1:8000/docs`

---

## Notes

* No real devices are required for development — mock mode simulates all operations
* The system is designed to scale to real execution without any API changes
* Audit logs are persisted to the database and survive service restarts
* All passwords are encrypted at rest and decrypted only at playbook execution time
