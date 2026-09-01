// Mirrors the backend `app.schemas.port.PortRead` schema.
// All fields are always present in responses; unknown values arrive as null.

export type PortMode = 'access' | 'trunk' | 'unknown';

export interface Port {
  name: string;
  description: string | null;
  admin_up: boolean | null;
  operational_up: boolean | null;
  mode: PortMode;
  access_vlan: number | null;
  allowed_vlans: number[] | null;
  poe_enabled: boolean | null;
  speed: string | null;
  duplex: string | null;
}

// Envelope returned by GET /api/v1/ports/?device=...
// (already unwrapped from the outer { success, data } by the API client.)
export interface PortListResponse {
  device: string;
  vendor: string | null;
  count: number;
  ports: Port[];
}

// Body for PATCH /api/v1/ports/description.  Empty description clears it.
export interface PortDescriptionUpdateRequest {
  device: string;
  interface: string;
  description: string;
}

// Body for PATCH /api/v1/ports/admin-state.
// enabled=true → `undo shutdown` / `no shutdown`; enabled=false → `shutdown`.
export interface PortAdminStateUpdateRequest {
  device: string;
  interface: string;
  enabled: boolean;
}

// Body for PATCH /api/v1/ports/access-vlan.
export interface PortAccessVlanUpdateRequest {
  device: string;
  interface: string;
  vlan_id: number;
}

// Body for PATCH /api/v1/ports/trunk-vlans.
export type TrunkVlanMode = 'replace' | 'add' | 'remove';

export interface PortTrunkVlansUpdateRequest {
  device: string;
  interface: string;
  mode: TrunkVlanMode;
  vlans: number[];
}

// Body for POST /api/v1/ports/access-mode. Replaces the old generic
// /ports/configure for this specific, well-defined operation.
export interface PortSetAccessModeRequest {
  device: string;
  interface: string;
  access_vlan: number;
}

// Body for POST /api/v1/ports/trunk-mode. native_vlan (PVID) and
// allowed_vlans always fully replace whatever the port had before (mode
// change, not add/remove — use PortAccessVlanUpdateRequest/
// PortTrunkVlansUpdateRequest to adjust either individually on a port
// that's already trunk).
export interface PortSetTrunkModeRequest {
  device: string;
  interface: string;
  native_vlan: number;
  allowed_vlans: number[];
}

// Response shape — matches the orchestration envelope used by VLAN endpoints,
// so the existing JobNotificationContext can track these jobs unchanged.
export interface PortJobResult {
  device: string;
  job_id: string;
  status?: string;
}

export interface PortOperationResult {
  group_job_id: string;
  jobs: PortJobResult[];
}
