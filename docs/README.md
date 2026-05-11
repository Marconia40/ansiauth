# Network Automation API

## Description

This project implements a REST API for multi-vendor network device automation.
The system abstracts vendor-specific configuration through a unified interface and uses Ansible as the automation engine.

It supports real execution against Cisco devices (tested with GNS3 + Cisco IOSv) and a mock mode for development and testing without real hardware.

---

## Architecture

```
User → API (FastAPI) → Auth / Rate Limit → Validations → Job (async) → Service → (Mock / Ansible) → Result
```

Main components:

| Component | Responsibility |
|---|---|
| **API (FastAPI)** | Exposes REST endpoints |
| **Auth (JWT + RBAC)** | Token-based authentication with role enforcement, refresh token rotation, brute-force protection |
| **Rate Limiter** | Sliding-window per-IP and per-user HTTP request throttling |
| **Validators** | Input validation logic |
| **Job System** | Async task execution with DB-persisted status and real-time tracking |
| **Services** | Business logic (VLAN, devices, users, audit, secrets, etc.) |
| **Ansible** | Real execution on network devices via ansible-runner |
| **Audit Logger** | Append-only, per-device, per-job action tracing with DB persistence |
| **Device Locks** | Per-device concurrency serialization |
| **Scheduler** | Background job for daily audit log purge |

---

## Requirements

* Python 3.10+
* pip
* virtualenv (recommended)
* ansible-runner
* A `.env` file — see **Environment Variables** below

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
pip install -r backend/requirements.txt
```

### 4. Install Ansible collections

```bash
ansible-galaxy collection install cisco.ios
ansible-galaxy collection install ansible.netcommon
```

### 5. Configure environment variables

Create a `.env` file inside `backend/`:

```env
# ── Required ────────────────────────────────────────────────────────────────
JWT_SECRET_KEY=<64-char random hex>          # generate: python -c "import secrets; print(secrets.token_hex(32))"
FERNET_KEY=<base64-urlsafe 32-byte key>      # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
BOOTSTRAP_ADMIN_PASSWORD=<12+ char password> # initial admin password on first boot

# ── Optional (defaults shown) ────────────────────────────────────────────────
BOOTSTRAP_ADMIN_USER=admin
EXECUTION_MODE=mock                          # "mock" (no devices) or "real" (live Ansible)
DATABASE_URL=sqlite:///./app.db
ACCESS_TOKEN_EXPIRE_MINUTES=15              # 1–15
REFRESH_TOKEN_EXPIRE_DAYS=7
AUDIT_RETENTION_DAYS=90
RATE_LIMIT_PER_IP=20                        # unauthenticated requests per minute per IP
RATE_LIMIT_PER_USER=200                     # authenticated requests per minute per user
RATE_LIMIT_LOGIN=5                          # login attempts per minute per IP
```

> **Key generation:** `JWT_SECRET_KEY` and `FERNET_KEY` are independent — one signs tokens, the other encrypts device passwords. Never reuse or swap them.

---

## Running the API

From the project root:

```bash
PYTHONPATH=backend uvicorn app.main:app --reload
```

Or from inside `backend/`:

```bash
cd backend/
uvicorn app.main:app --reload
```

API available at `http://127.0.0.1:8000`

Swagger UI (interactive docs): `http://127.0.0.1:8000/docs`

### First boot

On the first startup, if no active admin user exists and `BOOTSTRAP_ADMIN_PASSWORD` is set, the API automatically creates an admin user with the configured credentials. Check the log output for confirmation:

```
Bootstrap: created admin user 'admin' (id=1)
```

---

## Health Check

```
GET /health
```

Returns application and database status. **No authentication required.** Excluded from rate limiting.

```json
{
  "status": "ok",
  "db_status": "ok",
  "timestamp": "2026-05-11T17:00:00+00:00",
  "version": "1.0.0"
}
```

Returns `200` when healthy, `503` when the database is unreachable. Suitable for load balancer liveness probes.

---

## Authentication & Authorization

The API uses **short-lived JWT access tokens** paired with **single-use refresh tokens**.

### Login

```
POST /api/v1/auth/login
Content-Type: application/x-www-form-urlencoded
```

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=admin&password=<your_password>"
```

Returns:

```json
{
  "access_token": "<jwt>",
  "token_type": "bearer",
  "refresh_token": "<opaque_token>"
}
```

### Refresh access token

```
POST /api/v1/auth/refresh
```

```json
{ "refresh_token": "<token>" }
```

Returns a new `access_token` and a new `refresh_token` (the old one is immediately revoked). Replaying a revoked refresh token cascade-revokes **all sessions** for that user.

### Logout

```
POST /api/v1/auth/logout
```

```json
{ "refresh_token": "<token>" }
```

Revokes the refresh token. The access token remains valid until it expires (max 15 minutes).

### Brute force protection

The login endpoint is protected at two levels:

| Trigger | Limit | Window | Response |
|---|---|---|---|
| Failed attempts per username | 5 | 15 minutes | 429 |
| Failed attempts per IP | 20 | 1 hour | 429 |

An admin can manually unlock a locked username:

```
POST /api/v1/auth/unlock/{username}
```

### HTTP rate limiting

All non-health endpoints are subject to per-IP and per-user sliding-window limits:

| Scope | Default limit | Window |
|---|---|---|
| Unauthenticated (per IP) | 20 req/min | 60 s |
| Authenticated (per user) | 200 req/min | 60 s |
| Login endpoint (per IP) | 5 req/min | 60 s |

Exceeded limits return `429 Too Many Requests` with a `Retry-After` header. Limits are configurable via env vars (see **Environment Variables**).

### Roles

| Role | Permissions |
|---|---|
| `super-admin` | Full access including user management (create/update/delete any user) |
| `admin` | Device management, VLAN operations, audit log, create users up to admin role |
| `operator` | Execute configuration changes (VLAN operations) |
| `observer` | Read-only access |

Roles are hierarchical — higher roles include all permissions of lower roles.

### Swagger usage

Click the **Authorize** button in Swagger UI, enter `Bearer <access_token>`, and all requests will be authenticated automatically.

---

## Error Response Format

All `4xx` and `5xx` responses follow a standardized format:

```json
{
  "error_code": "NOT_FOUND",
  "message": "Device 'cisco99' not found",
  "details": null,
  "timestamp": "2026-05-11T17:00:00.123456+00:00"
}
```

| `error_code` | HTTP status |
|---|---|
| `UNAUTHORIZED` | 401 |
| `FORBIDDEN` | 403 |
| `NOT_FOUND` | 404 |
| `VALIDATION_ERROR` | 400 / 422 |
| `RATE_LIMIT_EXCEEDED` | 429 |
| `INTERNAL_ERROR` | 500 |

---

## User Management

Admins and super-admins can manage users through the API. Passwords are always hashed (pbkdf2_sha256) and are never returned in any response.

| Method | Endpoint | Min. role | Description |
|---|---|---|---|
| `POST` | `/api/v1/users/` | admin | Create user |
| `GET` | `/api/v1/users/` | admin | List users (paginated) |
| `GET` | `/api/v1/users/{id}` | admin | Get user |
| `PUT` | `/api/v1/users/{id}` | super-admin | Update user |
| `DELETE` | `/api/v1/users/{id}` | super-admin | Deactivate user |

**Safety guard:** The last active `super-admin` cannot be deactivated.

### Example — Create a user

```json
POST /api/v1/users/
{
  "username": "netops1",
  "password": "secure_password_123",
  "role": "operator"
}
```

---

## VLAN Management

### Supported operations

| Method | Endpoint | Min. role | Description |
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

All configuration operations are executed asynchronously. Jobs are persisted to the database and survive service restarts.

### Job lifecycle

```
pending → running → completed
                 → failed
                 → cancelled
```

Orphaned jobs (left in `running` state from a previous process) are automatically marked `failed` on startup.

### Job fields

| Field | Description |
|---|---|
| `job_id` | Unique identifier |
| `status` | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `current_step` | Real-time execution step: `retrying`, `completed`, `failed` |
| `retry_count` | Number of retry attempts performed so far |
| `max_retries` | Configured maximum retries (default: 3) |
| `last_error` | Most recent error message (updated on each retry) |
| `pre_state` | Device state captured before execution |
| `rollback_performed` | Whether a rollback was successfully executed |
| `error` | Final error message if the job failed |
| `result` | Output from the Ansible playbook on success |
| `created_at` | UTC timestamp of job creation |
| `started_at` | UTC timestamp when execution began |
| `finished_at` | UTC timestamp of completion |

### Check job status

```
GET /api/v1/jobs/{job_id}
```

During active retries, `retry_count`, `last_error`, and `current_step` are updated in real time.

### List jobs (with filtering and pagination)

```
GET /api/v1/jobs/?status=failed&device_id=cisco1&from_date=2026-05-01T00:00:00Z&page=1&page_size=50
```

| Parameter | Description |
|---|---|
| `status` | Filter by status: `pending`, `running`, `completed`, `failed`, `cancelled` |
| `device_id` | Filter by device name |
| `from_date` | ISO-8601 UTC start of date range |
| `to_date` | ISO-8601 UTC end of date range |
| `page` | Page number (default: 1) |
| `page_size` | Results per page (default: 50, max: 200) |

### Cancel a job

```
POST /api/v1/jobs/{job_id}/cancel
```

---

## Reliability & Production Safety

### Concurrency Control

Each device has an exclusive threading lock. Jobs targeting the same device are serialized — no two jobs run against the same device simultaneously. Jobs for different devices run in parallel.

### Smart Retry with Error Classification

When an Ansible playbook fails, the system classifies the error as **transient** or **permanent**:

**Transient errors** (trigger retry with exponential backoff):
* SSH connection refused / SSH failure / SSH error
* Connection timeout / Unable to connect
* Network unreachable / No route to host
* UNREACHABLE

**Permanent errors** (fail immediately, no retry):
* Configuration syntax errors
* Permission denied
* Any other unclassified error

Retry strategy:
* Maximum **3 retries**
* Exponential backoff: `1s → 2s → 4s`
* Error text checked in both `stderr` **and** `stdout` (Ansible places SSH errors in stdout)

### Safe Rollback

The system captures device state **before every operation**. If an operation fails, rollback is attempted based on that pre-state:

| Operation | Rollback action |
|---|---|
| `create_vlan` fails, VLAN did not exist before | Run `no vlan X` to clean up partial state |
| `update_vlan` fails | Restore original VLAN name |
| `delete_vlan` fails, VLAN existed before | Recreate VLAN with original name |

Rollback is **best-effort** — network devices are not transactional. Rollback success is tracked separately in `rollback_performed`.

---

## Device Management

Devices must be registered before being targeted by VLAN operations. Passwords are encrypted at rest using `FERNET_KEY`.

| Method | Endpoint | Min. role | Description |
|---|---|---|---|
| `POST` | `/api/v1/devices/` | admin | Register device |
| `GET` | `/api/v1/devices/` | admin | List devices |
| `GET` | `/api/v1/devices/{name}` | admin | Get device |
| `PUT` | `/api/v1/devices/{name}` | admin | Update device (re-encrypts password if changed) |
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

> **Vendor values:** Use `"cisco_ios"` for Cisco IOS devices. This maps to `ansible_network_os=ios` in the Ansible inventory.

---

## Secret Management

Device passwords are **never stored in plaintext**.

* Passwords are encrypted using `FERNET_KEY` before being saved to the database
* Decryption happens at runtime, only when a playbook is about to run
* `FERNET_KEY` must be set and never changed — losing or rotating it makes all stored passwords unrecoverable without re-registering devices
* `JWT_SECRET_KEY` is separate — it only signs tokens and can be rotated (all current tokens will be invalidated)

---

## Audit Logging

Every significant action is logged to the database. **Audit records are append-only** — once written, no row is ever updated. Status transitions create new linked rows via `parent_audit_id`.

### Audit log entry fields

| Field | Description |
|---|---|
| `user` | Who performed the action |
| `action` | What was done (e.g. `create_vlan`, `login`, `bootstrap_admin`) |
| `resource` | What was affected (`vlan`, `job`, `auth`, `user`, etc.) |
| `device` | Specific device targeted |
| `job_id` | Linked background job |
| `request_id` | Groups all entries from the same API request |
| `parent_audit_id` | Links to the preceding event in a status chain |
| `status` | `pending`, `completed`, `failed` |
| `details` | Additional context (JSON) |
| `timestamp` | UTC datetime |

### Query the audit log

```
GET /api/v1/audit/
GET /api/v1/audit/?user=operator
GET /api/v1/audit/?action=create_vlan
GET /api/v1/audit/?device_id=cisco1
GET /api/v1/audit/?from_date=2026-05-01T00:00:00Z&to_date=2026-05-11T23:59:59Z
GET /api/v1/audit/?resource=vlan&limit=50&skip=0
```

All filters can be combined. Requires `admin` role.

### Audit log retention

Old records are automatically purged daily at **02:00 UTC** based on `AUDIT_RETENTION_DAYS` (default: 90 days). An admin can also trigger a manual purge:

```
POST /api/v1/audit/purge
```

The purge action itself is recorded in the audit log.

---

## Ansible Integration

Playbooks are located in `backend/ansible/project/vendors/cisco/`. ansible-runner is used to execute them with per-device dynamic inventories.

Each playbook call receives:
* A dynamically built, **isolated** inventory string with device credentials (prevents race conditions during parallel execution)
* Extra vars (`vlan_id`, `vlan_name`, `device`)

**Execution Flow:**

1. API receives request and validates input
2. One job and one audit entry are created per device (synchronously, before any execution starts)
3. Background threads are dispatched per device
4. Per-device concurrency lock is acquired
5. Pre-state is captured from the device (`show vlan brief`)
6. Idempotency check runs (real mode only)
7. Ansible playbook executes with exponential backoff retry on transient errors
8. On failure, rollback is attempted using the captured pre-state
9. Job and audit are updated with final status, `pre_state`, `rollback_performed`, `retry_count`

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

### Ansible inventory configuration

The Ansible inventory is located at `ansible/inventory/inventory.ini`. Ensure `ansible/ansible.cfg` points to it:

```ini
[defaults]
inventory = inventory/inventory.ini
```

### Verify connectivity before running real jobs

```bash
cd ansible
ansible cisco -m ping
```

---

## Database Migrations (Alembic)

The project uses Alembic for versioned database migrations. Alembic configuration is at `backend/alembic.ini`; migration files are in `backend/migrations/versions/`.

```bash
cd backend

# Apply all migrations to bring DB up to date
alembic upgrade head

# Check current migration state
alembic current

# Generate a migration for new model changes
alembic revision --autogenerate -m "description"
```

> **Note:** The application also calls `Base.metadata.create_all()` on startup as a safety net, but Alembic is the authoritative migration tool and should be used for any schema changes.

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
| `test_jobs.py` | Job lifecycle, status transitions, filtering |
| `test_audit.py` | Audit persistence, filtering, multi-device rows |
| `test_devices.py` | Device registration and management |
| `test_multi_device_vlan.py` | Parallel execution, per-device jobs, inventory isolation |
| `test_reliability.py` | Concurrency locks, rollback, audit fields |
| `test_lifecycle.py` | Job/audit lifecycle consistency, stuck-state recovery |
| `test_vlan_semantics.py` | Idempotency: create existing (same/different name), update non-existent |
| `test_smart_retry.py` | Transient vs permanent error classification, retry count, real-time visibility |
| `test_rollback.py` | Create/update/delete rollback, `pre_state` on job and audit |
| `test_brute_force.py` | Login lockout by username and IP, unlock endpoint |
| `test_rate_limit.py` | Per-IP, per-user, per-login HTTP rate limiting, Retry-After header |
| `test_token_lifecycle.py` | Refresh token rotation, replay detection, cascade revocation, logout |
| `test_error_format.py` | Standardized error response shape across all error types (401–500) |
| `test_health.py` | Health endpoint contract, DB-down scenario, rate limiter exclusion |

---

## Current Project Status

| Feature | Status |
|---|---|
| VLAN create / delete / update | ✅ |
| Input validation | ✅ |
| Async job system (DB-persisted) | ✅ |
| Job filtering and pagination | ✅ |
| JWT authentication | ✅ |
| Token refresh with rotation | ✅ |
| Logout (token revocation) | ✅ |
| Brute force protection (username + IP lockout) | ✅ |
| HTTP rate limiting (per-IP and per-user) | ✅ |
| Role-based access control (RBAC) with super-admin | ✅ |
| User management API (CRUD) | ✅ |
| First-run admin bootstrap from env | ✅ |
| Device management (DB-backed) | ✅ |
| Device update endpoint | ✅ |
| Encrypted password storage (Fernet) | ✅ |
| Multi-device parallel execution | ✅ |
| Audit logging (append-only, per device + job) | ✅ |
| Audit date range and device filters | ✅ |
| Audit log retention policy with scheduler | ✅ |
| Request grouping via `request_id` | ✅ |
| Standardized error response format | ✅ |
| Health check endpoint (`/health`) | ✅ |
| Real Ansible execution | ✅ |
| GNS3 / Cisco IOSv integration | ✅ |
| Automated test suite (pytest, 16 test files) | ✅ |
| Database migrations (Alembic) | ✅ |
| Per-device concurrency locking | ✅ |
| Smart retry (transient vs permanent) | ✅ |
| Exponential backoff | ✅ |
| VLAN idempotency (pre-state check) | ✅ |
| Safe rollback with pre-state | ✅ |
| Orphaned job recovery on startup | ✅ |

---

## Limitations

* Cisco IOSv requires legacy SSH crypto configuration (see SSH section above)
* `FERNET_KEY` rotation is not supported — changing it invalidates all stored device passwords; devices must be re-registered
* Batch Ansible execution (single playbook run across multiple devices) is not supported — each device gets its own playbook invocation
* Only Cisco IOS (`cisco_ios`) is fully supported; Huawei driver is registered but not implemented (returns an error on use)

---

## Future Improvements

* Huawei VRP vendor driver (VLAN, port, and interface operations)
* Physical port management API (`/api/v1/devices/{id}/ports/`)
* Virtual interface (SVI / loopback) management API
* Global device configuration (hostname, SNMP, NTP, static routes, backup/restore)
* Device groups for bulk operations
* HTTPS/TLS enforcement (currently HTTP only)
* Prometheus metrics endpoint (`/metrics`)
* WebSocket / SSE support for real-time job status push (currently requires polling)
* `FERNET_KEY` rotation utility

---

## Team Usage

To replicate the environment:

1. Clone the repository
2. Create and activate a virtual environment: `python -m venv venv && source venv/bin/activate`
3. Install dependencies: `pip install -r backend/requirements.txt`
4. Install Ansible collections: `ansible-galaxy collection install cisco.ios ansible.netcommon`
5. Create `backend/.env` with all required variables (see **Environment Variables** section)
6. Run the API: `PYTHONPATH=backend uvicorn app.main:app --reload`
7. On first boot, the admin user is created automatically from `BOOTSTRAP_ADMIN_PASSWORD`
8. Login via `POST /api/v1/auth/login` to obtain a token, then test from Swagger at `http://127.0.0.1:8000/docs`

---

## Notes

* No real devices are required for development — `EXECUTION_MODE=mock` simulates all operations
* The system is designed to scale to real execution without any API changes
* Audit logs are append-only and persisted to the database; they survive service restarts
* Jobs are persisted to the database and survive service restarts; orphaned jobs are auto-recovered
* All device passwords are encrypted at rest with Fernet and decrypted only at playbook execution time
* Replaying a revoked refresh token cascade-revokes all active sessions for that user
* Error classification checks both `stderr` and `stdout` — Ansible places SSH errors in stdout
