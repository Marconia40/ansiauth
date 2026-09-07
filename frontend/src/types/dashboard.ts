// Mirror del schema en backend/app/schemas/dashboard.py.
// Endpoint: GET /api/v1/dashboard/summary?scope=...
//
// La detección de discrepancia de nombre de VLAN se hace en frontend:
// vlans.entries[i].names.length > 1 significa que ese VLAN ID tiene
// nombres distintos entre devices.

export type DashboardScopeKind = 'org' | 'site' | 'group' | 'device';

export interface DashboardScope {
  kind: DashboardScopeKind;
  id: number | null;
  name: string | null;
}

export interface DevicesSummary {
  total: number;
  by_vendor: Record<string, number>;
  sync_errors: number;
  last_sync_at: string | null;
  sync_in_progress_count: number;
}

export interface VlanSummaryEntry {
  id: number;
  names: string[];
}

export interface VlansSummary {
  unique_count: number;
  entries: VlanSummaryEntry[];
}

// Mismo shape para PortsSummary y SvisSummary.
export interface PortLikeSummary {
  total: number;
  up: number;
  down: number;
  shutdown: number;
}

export type PortsSummary = PortLikeSummary;
export type SvisSummary = PortLikeSummary;

export interface JobsSummary {
  window_days: number;
  total: number;
  by_status: Record<string, number>;
  rollback_performed_count: number;
}

export interface DashboardSummary {
  scope: DashboardScope;
  generated_at: string;
  devices: DevicesSummary;
  vlans: VlansSummary;
  ports: PortsSummary;
  svis: SvisSummary;
  jobs: JobsSummary;
}

// Params que acepta el endpoint.
export interface DashboardSummaryParams {
  scope: DashboardScopeKind;
  id?: number;
  name?: string;
  jobs_days?: number;
}
