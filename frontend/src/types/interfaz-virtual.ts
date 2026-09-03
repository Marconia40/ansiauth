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
  vlan_id: number;
  description?: string | null;
}

// Body for DELETE /api/v1/interfaces-virtuales/.
export interface InterfazVirtualDeleteRequest {
  vlan_id: number;
}

// Body for PATCH /api/v1/interfaces-virtuales/admin-state.
export interface InterfazVirtualAdminStateUpdateRequest {
  vlan_id: number;
  enabled: boolean;
}

// Body for PATCH /api/v1/interfaces-virtuales/description. Sets a description
// (value required). To clear it, use InterfazVirtualDescriptionClearRequest instead.
export interface InterfazVirtualDescriptionUpdateRequest {
  vlan_id: number;
  description: string;
}

// Body for DELETE /api/v1/interfaces-virtuales/description.
export interface InterfazVirtualDescriptionClearRequest {
  vlan_id: number;
}

// Body for PATCH /api/v1/interfaces-virtuales/ipv4. CIDR (e.g. "10.10.10.11/24"),
// value required. secondary=true targets the secondary IPv4 address instead
// of the primary (requires a primary already configured). To clear an
// address, use InterfazVirtualIpv4ClearRequest instead.
export interface InterfazVirtualIpv4UpdateRequest {
  vlan_id: number;
  ipv4_address: string;
  secondary?: boolean;
}

// Body for DELETE /api/v1/interfaces-virtuales/ipv4.
export interface InterfazVirtualIpv4ClearRequest {
  vlan_id: number;
  secondary?: boolean;
}

// Body for PATCH /api/v1/interfaces-virtuales/ipv6. CIDR (e.g. "2001:db8::1/64"),
// value required. To clear it, use InterfazVirtualIpv6ClearRequest instead.
export interface InterfazVirtualIpv6UpdateRequest {
  vlan_id: number;
  ipv6_address: string;
}

// Body for DELETE /api/v1/interfaces-virtuales/ipv6.
export interface InterfazVirtualIpv6ClearRequest {
  vlan_id: number;
}

export type InterfazVirtualAclDirection = 'in' | 'out';

// Body for PATCH /api/v1/interfaces-virtuales/acl. Binds an ACL that already
// exists on the device, acl_name required. To clear a binding, use
// InterfazVirtualAclClearRequest instead.
export interface InterfazVirtualAclUpdateRequest {
  vlan_id: number;
  direction: InterfazVirtualAclDirection;
  acl_name: string;
}

// Body for DELETE /api/v1/interfaces-virtuales/acl.
export interface InterfazVirtualAclClearRequest {
  vlan_id: number;
  direction: InterfazVirtualAclDirection;
}

// Body for POST /api/v1/interfaces-virtuales/dhcp-relay. Adds 1 server,
// incremental — leaves any other server already configured untouched.
export interface InterfazVirtualDhcpRelayAddRequest {
  vlan_id: number;
  server: string;
}

// Body for DELETE /api/v1/interfaces-virtuales/dhcp-relay. Removes 1 server,
// incremental — leaves any other server already configured untouched.
export interface InterfazVirtualDhcpRelayRemoveRequest {
  vlan_id: number;
  server: string;
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
