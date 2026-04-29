Implement a mock mode for VLAN execution in the project.

Goal:
Allow the system to run WITHOUT real network devices by simulating Ansible execution.

Requirements:

1. Modify vlan_service.py:
   - Add support for a MOCK_MODE using environment variables
   - If MOCK_MODE=true:
       - Do NOT execute real Ansible
       - Return a simulated response:
         {
           "rc": 0,
           "stdout": "Simulated VLAN <id> created",
           "stderr": ""
         }
   - If MOCK_MODE=false:
       - Keep current real execution (do not break existing logic)

2. Use Python standard approach:
   - import os
   - MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"

3. Add logging:
   - Log when running in mock mode
   - Log when running real execution

4. Simulate failure case:
   - If vlan_id == 999:
       return:
         {
           "rc": 1,
           "stdout": "",
           "stderr": "Simulated device failure"
         }

5. Ensure compatibility:
   - Do NOT modify API endpoints
   - Do NOT break job system
   - Keep return format identical

6. Optional (if clean):
   - Extract mock logic into a helper function

7. Follow project structure and coding style.

Explain changes before applying.
