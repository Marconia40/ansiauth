// Mirrors the backend `app.schemas.svi` schemas.
// All fields are always present in responses; unknown values arrive as null.

export interface SVI {
  vlan_id: number;
  description: string | null;
  admin_up: boolean | null;
  operational_up: boolean | null;
  ipv4_address: string | null;
  ipv4_address_secondary: string | null;
  ipv6_address: string | null;
  acl_in: string | null;
  acl_out: string | null;
  dhcp_relay_servers: string[] | null;
}

// Envelope returned by GET /api/v1/svis/?device=...
// (already unwrapped from the outer { success, data } by the API client.)
export interface SVIListResponse {
  device: string;
  vendor: string | null;
  count: number;
  svis: SVI[];
}

// Body for POST /api/v1/svis/. Creates the SVI for vlan_id
// (must already exist on the device — RF-INTERV-09, validated server-side).
// description is optional, applied atomically right after creation. If the
// interface already exists, the job reports it as a no-op duplicate.
export interface SVICreateRequest {
  vlan_id: number;
  description?: string | null;
}

// Body for DELETE /api/v1/svis/.
export interface SVIDeleteRequest {
  vlan_id: number;
}

// Body for PATCH /api/v1/svis/admin-state.
export interface SVIAdminStateUpdateRequest {
  vlan_id: number;
  enabled: boolean;
}

// Body for PATCH /api/v1/svis/description. Sets a description
// (value required). To clear it, use SVIDescriptionClearRequest instead.
export interface SVIDescriptionUpdateRequest {
  vlan_id: number;
  description: string;
}

// Body for DELETE /api/v1/svis/description.
export interface SVIDescriptionClearRequest {
  vlan_id: number;
}

// Body for PATCH /api/v1/svis/ipv4. CIDR (e.g. "10.10.10.11/24"),
// value required. secondary=true targets the secondary IPv4 address instead
// of the primary (requires a primary already configured). To clear an
// address, use SVIIpv4ClearRequest instead.
export interface SVIIpv4UpdateRequest {
  vlan_id: number;
  ipv4_address: string;
  secondary?: boolean;
}

// Body for DELETE /api/v1/svis/ipv4.
export interface SVIIpv4ClearRequest {
  vlan_id: number;
  secondary?: boolean;
}

// Body for PATCH /api/v1/svis/ipv6. CIDR (e.g. "2001:db8::1/64"),
// value required. To clear it, use SVIIpv6ClearRequest instead.
export interface SVIIpv6UpdateRequest {
  vlan_id: number;
  ipv6_address: string;
}

// Body for DELETE /api/v1/svis/ipv6.
export interface SVIIpv6ClearRequest {
  vlan_id: number;
}

export type SVIAclDirection = 'in' | 'out';

// Body for PATCH /api/v1/svis/acl. Binds an ACL that already
// exists on the device, acl_name required. To clear a binding, use
// SVIAclClearRequest instead.
export interface SVIAclUpdateRequest {
  vlan_id: number;
  direction: SVIAclDirection;
  acl_name: string;
}

// Body for DELETE /api/v1/svis/acl.
export interface SVIAclClearRequest {
  vlan_id: number;
  direction: SVIAclDirection;
}

// Body for POST /api/v1/svis/dhcp-relay. Adds 1 server,
// incremental — leaves any other server already configured untouched.
export interface SVIDhcpRelayAddRequest {
  vlan_id: number;
  server: string;
}

// Body for DELETE /api/v1/svis/dhcp-relay. Removes 1 server,
// incremental — leaves any other server already configured untouched.
export interface SVIDhcpRelayRemoveRequest {
  vlan_id: number;
  server: string;
}

// Response shape — matches the orchestration envelope used by VLAN/port endpoints,
// so the existing JobNotificationContext can track these jobs unchanged.
export interface SVIJobResult {
  device: string;
  job_id: string;
  status?: string;
}

export interface SVIOperationResult {
  group_job_id: string;
  jobs: SVIJobResult[];
}

// Body for PATCH /api/v1/devices/{name}/svis/{vlan_id}/batch. All fields
// optional together on one object (unlike each individual endpoint, which
// takes exactly one) — the server applies every set field in 1 SSH
// connection instead of 1 per field. `""` clears a field (same meaning it
// already has on every individual endpoint's clear variant), `null`/
// omitted leaves it untouched. DHCP relay has no batch equivalent — it
// stays immediate via POST/DELETE .../dhcp-relay. vlan_id is the URL
// path segment, not part of this body.
export interface SVIBatchRequest {
  description?: string | null;
  admin_up?: boolean | null;
  ipv4_address?: string | null;
  ipv4_address_secondary?: string | null;
  ipv6_address?: string | null;
  acl_in?: string | null;
  acl_out?: string | null;
}

// Response shape for PATCH .../svis/{vlan_id}/batch is the SAME
// SVIOperationResult as every other SVI endpoint — `jobs` always has
// exactly 1 entry here (1 batch = 1 connection = 1 job).
