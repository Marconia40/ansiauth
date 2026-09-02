// Mirrors the backend `app.schemas.interfaz_virtual` schemas.
// All fields are always present in responses; unknown values arrive as null.

export interface InterfazVirtual {
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

// Envelope returned by GET /api/v1/interfaces-virtuales/?device=...
// (already unwrapped from the outer { success, data } by the API client.)
export interface InterfazVirtualListResponse {
  device: string;
  vendor: string | null;
  count: number;
  interfaces_virtuales: InterfazVirtual[];
}

// Body for POST /api/v1/interfaces-virtuales/. Creates the SVI for vlan_id
// (must already exist on the device — RF-INTERV-09, validated server-side).
// description is optional, applied atomically right after creation. If the
// interface already exists, the job reports it as a no-op duplicate.
export interface InterfazVirtualCreateRequest {
  device: string;
  vlan_id: number;
  description?: string | null;
}

// Body for POST /api/v1/interfaces-virtuales/delete.
export interface InterfazVirtualDeleteRequest {
  device: string;
  vlan_id: number;
}

// Body for PATCH /api/v1/interfaces-virtuales/admin-state.
export interface InterfazVirtualAdminStateUpdateRequest {
  device: string;
  vlan_id: number;
  enabled: boolean;
}

// Body for PATCH /api/v1/interfaces-virtuales/description. Empty description clears it.
export interface InterfazVirtualDescriptionUpdateRequest {
  device: string;
  vlan_id: number;
  description: string;
}

// Body for PATCH /api/v1/interfaces-virtuales/ipv4. CIDR (e.g. "10.10.10.11/24");
// omitted/empty clears the address. secondary=true targets the secondary
// IPv4 address instead of the primary (requires a primary already configured).
export interface InterfazVirtualIpv4UpdateRequest {
  device: string;
  vlan_id: number;
  ipv4_address?: string | null;
  secondary?: boolean;
}

// Body for PATCH /api/v1/interfaces-virtuales/ipv6. CIDR (e.g. "2001:db8::1/64");
// omitted/empty clears the address.
export interface InterfazVirtualIpv6UpdateRequest {
  device: string;
  vlan_id: number;
  ipv6_address?: string | null;
}

export type InterfazVirtualAclDirection = 'in' | 'out';

// Body for PATCH /api/v1/interfaces-virtuales/acl. Binds an ACL that already
// exists on the device — omitted/empty acl_name clears the current binding.
export interface InterfazVirtualAclUpdateRequest {
  device: string;
  vlan_id: number;
  direction: InterfazVirtualAclDirection;
  acl_name?: string | null;
}

// Body for PATCH /api/v1/interfaces-virtuales/dhcp-relay. Incremental —
// exactly one of add/remove per call, not a full-replace of the list.
export interface InterfazVirtualDhcpRelayUpdateRequest {
  device: string;
  vlan_id: number;
  add?: string | null;
  remove?: string | null;
}

// Response shape — matches the orchestration envelope used by VLAN/port endpoints,
// so the existing JobNotificationContext can track these jobs unchanged.
export interface InterfazVirtualJobResult {
  device: string;
  job_id: string;
  status?: string;
}

export interface InterfazVirtualOperationResult {
  group_job_id: string;
  jobs: InterfazVirtualJobResult[];
}
