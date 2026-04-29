Use docs/engineering_rules.md as project guidelines.

I want to implement an asynchronous job system for network operations.

Current situation:
- FastAPI backend
- VLAN endpoint executes Ansible directly

Goal:
Decouple request from execution.

Requirements:

1. Create a job model:
   - job_id
   - status (pending, running, completed, failed)
   - result
   - created_at

2. Modify VLAN endpoint:
   - Instead of executing Ansible directly, create a job
   - Return job_id immediately

3. Create a background worker:
   - Execute Ansible playbook asynchronously
   - Update job status and result

4. Add new endpoint:
   - GET /api/v1/jobs/{job_id}
   - Return job status and result

5. Store jobs in memory (simple dict for now)

Constraints:
- Do not use external services (no Redis yet)
- Keep implementation simple and clean
- Do not modify unrelated modules

Show me the diff before applying changes.
