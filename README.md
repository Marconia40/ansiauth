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

```bash
uvicorn backend.app.main:app --reload
```

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

