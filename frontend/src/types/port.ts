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
  // null cuando el driver no pudo determinar el estado (parser no matcheo
  // la salida real del equipo, no inventar valor).
  storm_control_enabled: boolean | null;
  // Percent 0-100. Null aún con enabled=true cuando el equipo tiene storm
  // control en pps/bps u otra unidad no-percent (config preexistente).
  storm_control_threshold: number | null;
  // 'filter' (descarta el exceso, el puerto sigue arriba) o 'shutdown'
  // (el puerto se cae). Null cuando desconocido o storm control está
  // deshabilitado.
  storm_control_action: string | null;
  // Si manda trap SNMP, independiente de la acción. Null en las mismas
  // condiciones que storm_control_action.
  storm_control_trap: boolean | null;
}

// Envelope returned by GET /api/v1/ports/?device=...
// (already unwrapped from the outer { success, data } by the API client.)
export interface PortListResponse {
  device: string;
  vendor: string | null;
  count: number;
  ports: Port[];
}

// Body for PATCH /api/v1/ports/description.  Sets a description (value
// required). To clear it, use PortDescriptionClearRequest instead.
export interface PortDescriptionUpdateRequest {
  interface: string;
  description: string;
}

// Body for DELETE /api/v1/ports/description.
export interface PortDescriptionClearRequest {
  interface: string;
}

// Body for PATCH /api/v1/ports/admin-state.
// enabled=true → `undo shutdown` / `no shutdown`; enabled=false → `shutdown`.
export interface PortAdminStateUpdateRequest {
  interface: string;
  enabled: boolean;
}

// Body for PATCH /api/v1/ports/access-vlan.
export interface PortAccessVlanUpdateRequest {
  interface: string;
  vlan_id: number;
}

// Body for PATCH /api/v1/ports/trunk-vlans.
export type TrunkVlanMode = 'replace' | 'add' | 'remove';

export interface PortTrunkVlansUpdateRequest {
  interface: string;
  mode: TrunkVlanMode;
  vlans: number[];
}

// Body for POST /api/v1/ports/access-mode. Replaces the old generic
// /ports/configure for this specific, well-defined operation.
export interface PortSetAccessModeRequest {
  interface: string;
  access_vlan: number;
}

// Body for POST /api/v1/ports/trunk-mode. native_vlan (PVID) and
// allowed_vlans always fully replace whatever the port had before (mode
// change, not add/remove — use PortAccessVlanUpdateRequest/
// PortTrunkVlansUpdateRequest to adjust either individually on a port
// that's already trunk).
export interface PortSetTrunkModeRequest {
  interface: string;
  native_vlan: number;
  allowed_vlans: number[];
}

// Body for PATCH /api/v1/ports/poe (RF-PUERTO-09).
// enabled=true → `power inline auto` / `poe enable`; enabled=false → `power inline never` / `poe disable`.
export interface PortPoeUpdateRequest {
  interface: string;
  enabled: boolean;
}

// Body for PATCH /api/v1/ports/storm-control (RF-PUERTO-07). Simplified scope:
// one enable flag + one global percentage threshold, not the 3 traffic types
// real hardware exposes separately. threshold_percent is required when enabled=true.
export interface PortStormControlUpdateRequest {
  interface: string;
  enabled: boolean;
  threshold_percent?: number | null;
  // Solo tienen efecto con enabled=true; si se omiten, el server aplica
  // 'shutdown'/true (mismo comportamiento hardcodeado de siempre).
  action?: 'filter' | 'shutdown' | null;
  trap?: boolean | null;
}

// Body for POST /api/v1/ports/reset (RF-PUERTO-10). Resets the interface to
// its factory-default configuration — no extra fields beyond device+interface.
export interface PortResetRequest {
  interface: string;
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

// Body for POST /api/v1/devices/{name}/ports/batch. Each entry can carry
// the FULL config of one port at once (multiple fields together), not just
// one — the server groups them into as many device-side commands as
// needed but sends them all in 1 SSH connection instead of 1 per field.
// Same field set as the individual per-field requests above, just all
// optional together on one object. dhcp relay has no port-level
// equivalent (svi-only), not included here.
export interface PortBatchChangeItem {
  interface: string;
  description?: string | null;
  admin_up?: boolean | null;
  mode?: 'access' | 'trunk' | null;
  access_vlan?: number | null;
  allowed_vlans?: number[] | null;
  allowed_vlan_operation?: TrunkVlanMode;
  poe_enabled?: boolean | null;
  storm_control_enabled?: boolean | null;
  storm_control_threshold?: number | null;
  storm_control_action?: 'filter' | 'shutdown' | null;
  storm_control_trap?: boolean | null;
}

export interface PortBatchRequest {
  changes: PortBatchChangeItem[];
}

// Response shape for POST .../ports/batch is the SAME PortOperationResult
// as every other port endpoint (`{group_job_id, jobs}`) — `jobs` just
// always has exactly 1 entry here (1 batch = 1 connection = 1 job), vs.
// potentially N on the individual endpoints (1 per device fan-out).
