// Mirrors the backend `app.schemas.global_config` schemas.
// All fields on read responses may arrive as null when the device didn't
// report them or the cache is empty.

// ── Read shapes ──────────────────────────────────────────────────────────────

export interface GlobalConfigSnmpInfo {
  enabled: boolean | null;
  version: string | null;
  community: string | null;
  permission: string | null;
  trap_hosts: string[] | null;
}

export interface GlobalConfigNtpInfo {
  servers: string[] | null;
}

export interface GlobalConfigDnsInfo {
  servers: string[] | null;
}

export interface GlobalConfigLoggingInfo {
  servers: string[] | null;
  level: string | null;
}

export interface GlobalConfigRouteEntry {
  // Kept loose (record<string, unknown>) because the parser emits vendor-shaped
  // rows — destination/next_hop/interface are always there, but individual
  // fields may be null depending on route type (connected vs static).
  destination?: string | null;
  next_hop?: string | null;
  interface?: string | null;
  [k: string]: unknown;
}

export interface GlobalConfigAclInfo {
  name: string;
  type: string | null;
  // Raw rule lines as the device prints them (1 line per rule).
  rules: string[];
}

// Payload returned by GET /devices/{name}/global-config/. The `device` +
// `vendor` fields are added by the endpoint (not on the pydantic model).
export interface GlobalConfigRead {
  device: string;
  vendor: string | null;
  hostname: string | null;
  snmp: GlobalConfigSnmpInfo;
  ntp: GlobalConfigNtpInfo;
  dns: GlobalConfigDnsInfo;
  logging: GlobalConfigLoggingInfo;
  routes: GlobalConfigRouteEntry[] | null;
  acls: GlobalConfigAclInfo[] | null;
}

// 1 device inside DashboardSummary.global_config (GET /dashboard/summary
// with ?include_global_config=true). Same shape as GlobalConfigRead plus
// sync metadata — mirrors backend/app/schemas/dashboard.py
// GlobalConfigScopeDeviceEntry. `routes` was omitted from the first pass
// of the cross-device view; the backend payload already includes it
// (confirmed live), this is just catching the frontend type up.
export interface GlobalConfigScopeDeviceEntry {
  device: string;
  vendor: string | null;
  hostname: string | null;
  snmp: GlobalConfigSnmpInfo;
  ntp: GlobalConfigNtpInfo;
  dns: GlobalConfigDnsInfo;
  logging: GlobalConfigLoggingInfo;
  routes: GlobalConfigRouteEntry[] | null;
  acls: GlobalConfigAclInfo[] | null;
  synced_at: string | null;
  sync_error: string | null;
  sync_in_progress: boolean;
}

export interface GlobalConfigVersionRead {
  device: string;
  vendor: string | null;
  software_version: string | null;
  model: string | null;
  uptime: string | null;
}

export interface GlobalConfigRunningConfigRead {
  device: string;
  vendor: string | null;
  running_config: string[] | null;
}

// Rows come back as {ip, mac, interface, vlan, type, age, ...} for ARP
// and {mac, vlan, interface, type} for MAC. Kept as record because vendors
// diverge and the endpoint returns whatever the parser produced.
export type ArpMacEntry = Record<string, string | number | null>;

export interface ArpTableRead {
  device: string;
  vendor: string | null;
  entries: ArpMacEntry[] | null;
}

export interface MacTableRead {
  device: string;
  vendor: string | null;
  entries: ArpMacEntry[] | null;
}

export interface DeviceLogsRead {
  device: string;
  vendor: string | null;
  log_lines: string[] | null;
}

// ── Write request bodies ─────────────────────────────────────────────────────

export interface HostnameUpdateRequest {
  hostname: string;
}

export interface SnmpUpdateRequest {
  version?: string;
  community?: string;
  trap_source?: string;
  // trap_host + trap_version must be provided together (server-side validator).
  trap_host?: string;
  trap_version?: string;
}

export interface LogServerAddRequest {
  server: string;
  level?: string;
}

export interface LogServerRemoveRequest {
  server: string;
}

export interface RouteAddRequest {
  destination: string;
  next_hop: string;
}

export type RouteRemoveRequest = RouteAddRequest;

export interface NtpAddRequest {
  server: string;
  prefer?: boolean;
}

export interface NtpRemoveRequest {
  server: string;
}

// Exactly one of `server` OR `domain_name` must be provided.
export interface DnsAddRequest {
  server?: string;
  domain_name?: string;
}

export interface DnsRemoveRequest {
  server: string;
}

// ACL rule shape — vendor-agnostic. Exactly 1 of any/host/network per
// endpoint (source/destination). port only when operator == 'range' needs value2.
export interface AclRuleEndpoint {
  any?: boolean;
  host?: string;
  network?: string;
}

export interface AclRulePort {
  operator: 'eq' | 'range';
  value: string;
  value2?: string;
}

export interface AclRule {
  action: 'permit' | 'deny';
  protocol: string;
  source: AclRuleEndpoint;
  destination: AclRuleEndpoint;
  port?: AclRulePort;
}

export interface AclCreateRequest {
  name: string;
  rules: AclRule[];
}

export type AclRuleRemoveRequest = AclCreateRequest;

export interface AclDeleteRequest {
  name: string;
}

// ── Operation result — same envelope as VLAN/SVI/Port writes ─────────────────

export interface GlobalConfigJobResult {
  device: string;
  job_id: string;
  status?: string;
}

export interface GlobalConfigOperationResult {
  group_job_id: string;
  jobs: GlobalConfigJobResult[];
}
