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
