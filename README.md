# Network Automation API

## Overview

This project provides a REST API for centralized management of multi-vendor network devices (Cisco, Huawei, etc.) using Ansible as the automation engine.

The goal is to abstract vendor-specific configurations and allow operators to perform common network tasks through a unified interface.

---

## Architecture

Client → FastAPI → Ansible Runner → Playbooks → Network Devices

---

## Features (MVP)

- VLAN management (create, delete, list)
- Port configuration (access, trunk)
- Multi-device operations
- Role-based access (planned)
- Audit logging (planned)

---

## Project Structure

network-automation-api/\
|\
├── backend/ # FastAPI application\
├── ansible/ # Playbooks and inventory\
├── scripts/ # Testing scripts\
├── worker/ # Async jobs (future)\

---

## Requirements

- Python 3.10+
- Ansible
- SSH access to network devices

---

## Installation

### 1. Clone the repository

```bash
git clone <your-repo-url > 
cd ansiauth
```

### 2. Create virtual environment

```bash
python3 -m venv venv
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
ansible-galaxy collection install community.network
```

## Configuration

Create a .env file in the root directory:
ANSIBLE_USER=your_user
ANSIBLE_PASSWORD=your_password

⚠️ Do not commit this file.

## Inventory

An example inventory is provided:

- ansible/inventory/inventory.example.ini

You must create your own:

- ansible/inventory/inventory.ini

## Running the API

The backend talks to **either SQLite (default, dev) or PostgreSQL 16 (recommended for staging/prod)**.
Pick a path:

### Option A — Local SQLite (no containers)

```bash
cd backend
alembic upgrade head        # one-time per fresh DB
uvicorn app.main:app --reload
```

The default `DATABASE_URL` is a file at `backend/app/db/app.db`. No extra config needed.

### Option B — PostgreSQL via docker-compose

A `docker-compose.yml` at the repo root brings up Postgres 16 + the backend:

```bash
docker compose up -d            # start db + backend
docker compose logs -f backend  # tail backend logs
docker compose down             # stop, keep data
docker compose down -v          # stop and wipe the postgres volume
```

The compose file:
- Builds the backend from `backend/Dockerfile` (Python 3.12 + Ansible).
- Runs `alembic upgrade head` automatically before starting uvicorn.
- Exposes the API on `http://localhost:8000` and Postgres on `localhost:5432`.
- Stores DB data in the named volume `ansiauth-postgres-data`.

Overrides (env vars or a root-level `.env`):

| Variable                   | Default                                                              |
|----------------------------|----------------------------------------------------------------------|
| `JWT_SECRET_KEY`           | dev placeholder — **set this in production**                         |
| `FERNET_KEY`               | empty — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `BOOTSTRAP_ADMIN_PASSWORD` | `bootstrap_dev_password_123` — change before first boot              |
| `EXECUTION_MODE`           | `mock`                                                               |

### Option C — Existing PostgreSQL (no compose)

Point any `DATABASE_URL` you like at an external Postgres instance:

```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"
cd backend
alembic upgrade head
uvicorn app.main:app --reload
```

### Running the test suite against either backend

```bash
# SQLite (default)
cd backend && pytest

# Postgres — spin up a one-off container, point pytest at it
podman run -d --rm --name pg-test \
  -e POSTGRES_DB=ansiauth_test -e POSTGRES_USER=ansiauth -e POSTGRES_PASSWORD=ansiauth_dev \
  -p 55432:5432 postgres:16-alpine

DATABASE_URL="postgresql+psycopg://ansiauth:ansiauth_dev@127.0.0.1:55432/ansiauth_test" \
  pytest
```

`conftest.py` detects a Postgres URL and drops/recreates the `public` schema
on each session so runs are deterministic.

## Example Request
```bash
curl -X POST http://localhost:8000/api/v1/vlans \
-H "Content-Type: application/json" \
-d '{
  "vlan_id": 100,
  "vlan_name": "SALES",
  "target_hosts": ["sw1"]
}'
```


## Security Notes

-   Do not commit credentials
-   Use environment variables or secret management
-   Restrict API access in production

## Roadmap

-   Job queue (Celery)
-   Authentication (FreeRADIUS integration)
-   NetBox integration (inventory)
-   Web frontend

