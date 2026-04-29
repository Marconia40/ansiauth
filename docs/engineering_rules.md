# Engineering Rules - Network Automation API

## Project Overview

This project is a network automation platform built with:

- FastAPI (backend API)
- Ansible (automation engine)
- Multi-vendor support (Cisco, Huawei, future expansion)

The system abstracts vendor-specific CLI into a unified API.

---

## Architecture Principles

- The API is the single entry point for all operations
- No direct user access to network devices
- All configuration changes go through Ansible
- Validation MUST occur before executing any playbook
- The system must be modular and extensible

---

## Code Structure

- `api/` → FastAPI routes (no business logic)
- `schemas/` → Pydantic models (basic validation only)
- `validators/` → All validation logic (CRITICAL layer)
- `services/` → Business logic + Ansible execution

Rules:
- Do NOT put business logic in API routes
- Do NOT call Ansible directly from routes
- Validators must be reusable and independent

---

## Validation Rules

Before executing any operation:

- Validate input format (Pydantic)
- Validate business logic (validators)
- Reject invalid requests BEFORE reaching Ansible

Examples:
- VLAN ID must be 1–4094
- Reserved VLANs must not be used
- IP addresses must be valid
- Configurations must be logically consistent

---

## Error Handling

- Validation errors → HTTP 400
- Execution errors → HTTP 500
- Always return structured responses

Never expose raw system errors directly.

---

## Ansible Integration

- Use playbooks located in `ansible/playbooks/`
- Do not hardcode commands in Python
- Always pass variables via `-e`
- Limit execution to target device/group

---

## Security Principles

- Authentication is handled via API (JWT)
- Authorization is role-based (RBAC)
- No credentials stored in code
- Use environment variables or secure storage

---

## Coding Guidelines

- Keep functions small and focused
- Use clear naming (no abbreviations)
- Avoid duplicated logic
- Prefer explicit over implicit

---

## What NOT to do

- Do not bypass validators
- Do not mix layers (API, service, validation)
- Do not execute changes without validation
- Do not introduce breaking changes without explanation

---

## Expected Behavior from Claude

When modifying code:

- Respect existing structure
- Show diff before applying changes
- Do not modify unrelated files
- Keep changes minimal and clear
