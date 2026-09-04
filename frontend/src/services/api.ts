import axios from 'axios';
import type { AuthUser } from '@/types/auth';
import type { VlanEntry, VlanCreate, VlanUpdate, VlanDelete, VlanOperationResult } from '@/types/vlan';
import type { Device, DeviceCreate, DeviceUpdate } from '@/types/device';
import type {
  RoleAssignment,
  RoleAssignmentCreate,
  User,
  UserCreate,
  UserUpdate,
} from '@/types/user';
import type { Job, GroupJob } from '@/types/job';
import type { AuditLog } from '@/types/audit';
import type { Site, SiteCreate, SiteUpdate } from '@/types/site';
import type {
  PortAccessVlanUpdateRequest,
  PortAdminStateUpdateRequest,
  PortDescriptionClearRequest,
  PortDescriptionUpdateRequest,
  PortListResponse,
  PortOperationResult,
  PortPoeUpdateRequest,
  PortResetRequest,
  PortSetAccessModeRequest,
  PortSetTrunkModeRequest,
  PortStormControlUpdateRequest,
  PortTrunkVlansUpdateRequest,
} from '@/types/port';
import type {
  SVIAclClearRequest,
  SVIAclUpdateRequest,
  SVIAdminStateUpdateRequest,
  SVICreateRequest,
  SVIDeleteRequest,
  SVIDescriptionClearRequest,
  SVIDescriptionUpdateRequest,
  SVIDhcpRelayAddRequest,
  SVIDhcpRelayRemoveRequest,
  SVIIpv4ClearRequest,
  SVIIpv4UpdateRequest,
  SVIIpv6ClearRequest,
  SVIIpv6UpdateRequest,
  SVIListResponse,
  SVIOperationResult,
} from '@/types/svi';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

// ── API Response Wrapper ──────────────────────────────────────────────────────

type ApiResponse<T> = {
  success: boolean;
  data: T;
};

// ── Token storage ─────────────────────────────────────────────────────────────

let _accessToken: string | null = null;

export function setAccessToken(access: string): void {
  _accessToken = access;
  scheduleProactiveRefresh();
}

export function clearAccessToken(): void {
  _accessToken = null;
  cancelProactiveRefresh();
}

function parseAccessTokenExpiryMs(token: string): number | null {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return typeof payload.exp === 'number' ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

// ── Session expiration notification ───────────────────────────────────────────

const SESSION_EXPIRED_FLAG = 'ansiauth.sessionExpired';

let _sessionExpiredHandler: (() => void) | null = null;

export function onSessionExpired(handler: () => void): () => void {
  _sessionExpiredHandler = handler;
  return () => {
    if (_sessionExpiredHandler === handler) _sessionExpiredHandler = null;
  };
}

function notifySessionExpired(): void {
  cancelProactiveRefresh();
  _accessToken = null;
  if (typeof window !== 'undefined') {
    try {
      window.sessionStorage.setItem(SESSION_EXPIRED_FLAG, '1');
    } catch {
      /* ignore quota / disabled storage */
    }
  }
  _sessionExpiredHandler?.();
}

export function consumeSessionExpiredFlag(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    const flag = window.sessionStorage.getItem(SESSION_EXPIRED_FLAG);
    if (flag === '1') {
      window.sessionStorage.removeItem(SESSION_EXPIRED_FLAG);
      return true;
    }
  } catch {
    /* ignore */
  }
  return false;
}

// ── Axios instance ────────────────────────────────────────────────────────────

const client = axios.create({
  baseURL: `${BASE_URL}/api/v1`,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: true,
});

client.interceptors.request.use((config) => {
  if (_accessToken) {
    config.headers.Authorization = `Bearer ${_accessToken}`;
  }
  return config;
});

// ── Refresh coordination ──────────────────────────────────────────────────────

let _refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  if (_refreshInFlight) return _refreshInFlight;

  _refreshInFlight = (async () => {
    try {
      const { data } = await axios.post<{ access_token: string }>(
        `${BASE_URL}/api/v1/auth/refresh`,
        undefined,
        { withCredentials: true },
      );
      _accessToken = data.access_token;
      scheduleProactiveRefresh();
      return data.access_token;
    } finally {
      _refreshInFlight = null;
    }
  })();

  return _refreshInFlight;
}

// ── Proactive refresh scheduler ───────────────────────────────────────────────
//
// Schedules a refresh shortly before the access token's `exp`. This ensures the
// session terminates predictably even when the user is idle: when the refresh
// token also expires, the proactive refresh fails and we trigger a clean logout
// rather than waiting for the next API call.

let _refreshTimer: ReturnType<typeof setTimeout> | null = null;

function scheduleProactiveRefresh(): void {
  cancelProactiveRefresh();
  if (typeof window === 'undefined' || !_accessToken) return;

  const expiryMs = parseAccessTokenExpiryMs(_accessToken);
  if (!expiryMs) return;

  const leadMs = 30_000;
  const minDelay = 5_000;
  const maxDelay = 60 * 60_000;
  const delay = Math.max(minDelay, Math.min(expiryMs - Date.now() - leadMs, maxDelay));

  _refreshTimer = setTimeout(() => {
    refreshAccessToken().catch(() => {
      notifySessionExpired();
    });
  }, delay);
}

function cancelProactiveRefresh(): void {
  if (_refreshTimer !== null) {
    clearTimeout(_refreshTimer);
    _refreshTimer = null;
  }
}

// ── Focus / visibility revalidation ───────────────────────────────────────────
//
// Background tabs throttle setTimeout and OS sleep can suspend it entirely, so
// the proactive timer can be late by hours after a resume. When the tab becomes
// visible or window focuses, re-check whether the access token is still valid;
// if not, kick a refresh (deduped by _refreshInFlight) and let the existing
// failure path (notifySessionExpired) handle a stale refresh token.

const REVALIDATE_MARGIN_MS = 5_000;

function revalidateOnResume(): void {
  if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
  if (!_accessToken) return;

  const expiryMs = parseAccessTokenExpiryMs(_accessToken);
  if (expiryMs === null) return;

  // Access token still comfortably valid → nothing to do.
  if (expiryMs - Date.now() > REVALIDATE_MARGIN_MS) return;

  refreshAccessToken().catch(() => {
    notifySessionExpired();
  });
}

export function registerSessionRevalidation(): () => void {
  if (typeof window === 'undefined') return () => {};

  window.addEventListener('focus', revalidateOnResume);
  document.addEventListener('visibilitychange', revalidateOnResume);

  return () => {
    window.removeEventListener('focus', revalidateOnResume);
    document.removeEventListener('visibilitychange', revalidateOnResume);
  };
}

// ── 401 interceptor — single refresh + retry ──────────────────────────────────

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;

    if (!original || error.response?.status !== 401 || original._retry) {
      return Promise.reject(error);
    }

    // Don't try to refresh in response to a failing refresh request itself.
    if (typeof original.url === 'string' && original.url.includes('/auth/refresh')) {
      return Promise.reject(error);
    }

    original._retry = true;

    try {
      const newToken = await refreshAccessToken();
      original.headers = original.headers ?? {};
      original.headers.Authorization = `Bearer ${newToken}`;
      return client(original);
    } catch {
      notifySessionExpired();
      return Promise.reject(error);
    }
  },
);

// ── Generic API helper ────────────────────────────────────────────────────────

async function unwrap<T>(promise: Promise<{ data: ApiResponse<T> }>): Promise<T> {
  const { data } = await promise;
  return data.data;
}

// Single shared implementation, replacing 10 near-identical local copies.
// Prefers the first Pydantic validation message (RequestValidationError's
// `details.errors[]`, see backend/app/main.py) over the generic
// "Request validation failed" summary in `message`.
export function extractMessage(error: unknown, fallback: string): string {
  const e = error as {
    response?: {
      data?: {
        message?: string;
        details?: { errors?: Array<{ msg?: string }> };
      };
    };
    message?: string;
  } | null;
  const validationMsg = e?.response?.data?.details?.errors?.[0]?.msg;
  return validationMsg ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export async function login(
  username: string,
  password: string,
): Promise<AuthUser> {
  const form = new URLSearchParams({ username, password });

  const { data } = await axios.post<{ access_token: string }>(
    `${BASE_URL}/api/v1/auth/login`,
    form,
    {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      withCredentials: true,
    },
  );

  setAccessToken(data.access_token);

  return parseAuthUser(data.access_token);
}

// Post-Phase-5 JWTs carry ``is_system_admin`` instead of a legacy ``role``
// claim. UI code that reads ``user.role`` gets ``admin`` for system-admins
// and ``observer`` otherwise — per-scope permissions live in role_assignments
// and are fetched separately.
function parseAuthUser(token: string): AuthUser {
  const payload = JSON.parse(atob(token.split('.')[1]));
  const isSystemAdmin = Boolean(payload.is_system_admin);
  const role = payload.role ?? (isSystemAdmin ? 'admin' : 'observer');
  return {
    username: payload.sub,
    role,
    is_system_admin: isSystemAdmin,
  };
}

export async function restoreSession(): Promise<AuthUser | null> {
  try {
    const token = await refreshAccessToken();
    return parseAuthUser(token);
  } catch {
    clearAccessToken();
    return null;
  }
}

export async function logout(): Promise<void> {
  await client.post('/auth/logout').catch(() => {});
  clearAccessToken();
  if (typeof window !== 'undefined') {
    try {
      window.sessionStorage.removeItem(SESSION_EXPIRED_FLAG);
    } catch {
      /* ignore */
    }
  }
}

// ── VLANs ─────────────────────────────────────────────────────────────────────

/**
 * Envelope wrapping any cached device-scraped resource. `synced_at` is the
 * timestamp of the last successful scrape (may be null on brand-new devices);
 * `sync_error` carries the message from the most recent failed scrape and is
 * null on success; `sync_in_progress` flips true while a Celery refresh task
 * is running.
 */
export interface SyncedResource<T> {
  data: T;
  synced_at: string | null;
  sync_error: string | null;
  sync_in_progress: boolean;
}

export async function getVlans(device?: string): Promise<VlanEntry[]> {
  const envelope = await unwrap<SyncedResource<VlanEntry[]>>(
    client.get<ApiResponse<SyncedResource<VlanEntry[]>>>('/vlans/', {
      params: device ? { device } : {},
    }),
  );
  return envelope.data;
}

/** Returns the full envelope, keeping freshness metadata. Preferred for
 * dashboards that render a "Last synced Xm ago" indicator. */
export async function getVlansSynced(device: string): Promise<SyncedResource<VlanEntry[]>> {
  return unwrap<SyncedResource<VlanEntry[]>>(
    client.get<ApiResponse<SyncedResource<VlanEntry[]>>>('/vlans/', {
      params: { device },
    }),
  );
}

/** Fires the async Celery refresh for a device's VLAN cache. Returns as soon
 * as the task is queued (backend responds 202) — callers poll getVlansSynced
 * watching `sync_in_progress` to know when the DB is fresh. */
export async function refreshDeviceVlans(
  device: string,
): Promise<{ device: string; scope: 'vlans'; task_id: string }> {
  return unwrap<{ device: string; scope: 'vlans'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'vlans'; task_id: string }>>(
      `/devices/${device}/vlans/refresh`,
    ),
  );
}


export async function createVlan(body: VlanCreate): Promise<VlanOperationResult> {
  const result = await unwrap<VlanOperationResult>(client.post<ApiResponse<VlanOperationResult>>('/vlans/', body));
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function updateVlan(vlanId: number, body: VlanUpdate): Promise<VlanOperationResult> {
  const result = await unwrap<VlanOperationResult>(
    client.patch<ApiResponse<VlanOperationResult>>(`/vlans/${vlanId}`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function deleteVlan(vlanId: number, body: VlanDelete): Promise<VlanOperationResult> {
  const result = await unwrap<VlanOperationResult>(
    client.delete<ApiResponse<VlanOperationResult>>(`/vlans/${vlanId}`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

// ── Ports ─────────────────────────────────────────────────────────────────────

export async function getPorts(device: string): Promise<PortListResponse> {
  const envelope = await unwrap<SyncedResource<PortListResponse>>(
    client.get<ApiResponse<SyncedResource<PortListResponse>>>(`/devices/${device}/ports/`),
  );
  return envelope.data;
}

/** Full envelope variant — see `getVlansSynced` for the motivation. */
export async function getPortsSynced(
  device: string,
): Promise<SyncedResource<PortListResponse>> {
  return unwrap<SyncedResource<PortListResponse>>(
    client.get<ApiResponse<SyncedResource<PortListResponse>>>(`/devices/${device}/ports/`),
  );
}

export async function refreshDevicePorts(
  device: string,
): Promise<{ device: string; scope: 'ports'; task_id: string }> {
  return unwrap<{ device: string; scope: 'ports'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'ports'; task_id: string }>>(
      `/devices/${device}/ports/refresh`,
    ),
  );
}


export async function updatePortDescription(
  device: string,
  body: PortDescriptionUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/description`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function clearPortDescription(
  device: string,
  body: PortDescriptionClearRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.delete<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/description`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortAdminState(
  device: string,
  body: PortAdminStateUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/admin-state`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortAccessVlan(
  device: string,
  body: PortAccessVlanUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/access-vlan`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setTrunkAllowedVlans(
  device: string,
  body: PortTrunkVlansUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/trunk-vlans`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortAccessMode(
  device: string,
  body: PortSetAccessModeRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.post<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/access-mode`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortTrunkMode(
  device: string,
  body: PortSetTrunkModeRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.post<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/trunk-mode`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortPoe(
  device: string,
  body: PortPoeUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/poe`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setPortStormControl(
  device: string,
  body: PortStormControlUpdateRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.patch<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/storm-control`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function resetPort(
  device: string,
  body: PortResetRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.post<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/reset`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

// ── Virtual interfaces (SVI) ─────────────────────────────────────────────────

export async function getInterfacesVirtuales(device: string): Promise<SVIListResponse> {
  // Cache-first, same SyncedResource envelope as getPorts()/getVlans().
  const envelope = await unwrap<{ data: SVIListResponse }>(
    client.get<ApiResponse<{ data: SVIListResponse }>>(`/devices/${device}/svis/`),
  );
  return envelope.data;
}

export async function createSVI(
  device: string,
  body: SVICreateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.post<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function deleteSVI(
  device: string,
  body: SVIDeleteRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setSVIAdminState(
  device: string,
  body: SVIAdminStateUpdateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/admin-state`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setSVIDescription(
  device: string,
  body: SVIDescriptionUpdateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/description`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function clearSVIDescription(
  device: string,
  body: SVIDescriptionClearRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/description`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setSVIIpv4(
  device: string,
  body: SVIIpv4UpdateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv4`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function clearSVIIpv4(
  device: string,
  body: SVIIpv4ClearRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv4`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setSVIIpv6(
  device: string,
  body: SVIIpv6UpdateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv6`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function clearSVIIpv6(
  device: string,
  body: SVIIpv6ClearRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv6`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setSVIAcl(
  device: string,
  body: SVIAclUpdateRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/acl`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function clearSVIAcl(
  device: string,
  body: SVIAclClearRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/acl`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function addSVIDhcpRelay(
  device: string,
  body: SVIDhcpRelayAddRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.post<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/dhcp-relay`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeSVIDhcpRelay(
  device: string,
  body: SVIDhcpRelayRemoveRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/dhcp-relay`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

// ── Devices ───────────────────────────────────────────────────────────────────

export async function getDevices(): Promise<Device[]> {
  return unwrap(client.get<ApiResponse<Device[]>>('/devices/'));
}

export async function getDevice(name: string): Promise<Device> {
  return unwrap(client.get<ApiResponse<Device>>(`/devices/${name}`));
}

export async function createDevice(body: DeviceCreate) {
  return unwrap(client.post('/devices/', body));
}

export async function updateDevice(name: string, body: DeviceUpdate) {
  return unwrap(client.put(`/devices/${name}`, body));
}

export async function deleteDevice(name: string) {
  return unwrap(client.delete(`/devices/${name}`));
}

/**
 * MSP: Phase 4 — the sole authoritative way to change a device's group or
 * site. Pass `null` for `device_group_id` to move the device to its
 * current site's Default group (the D8 "remove from group" action).
 */
export async function moveDevice(
  name: string,
  deviceGroupId: number | null,
): Promise<Device> {
  return unwrap(
    client.post<ApiResponse<Device>>(`/devices/${name}/move`, {
      device_group_id: deviceGroupId,
    }),
  );
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

export async function getJobs(params?: {
  status?: string;
  device?: string;
  site_id?: number;
  from_date?: string;
  to_date?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: Job[]; total: number }> {
  const data = await unwrap<{ items: Job[]; total: number }>(
    client.get<ApiResponse<{ items: Job[]; total: number }>>('/jobs/', { params }),
  );
  return { items: data.items ?? [], total: data.total ?? 0 };
}

export async function getJob(jobId: string): Promise<Job> {
  return unwrap(client.get<ApiResponse<Job>>(`/jobs/${jobId}`));
}

export async function cancelJob(jobId: string) {
  return unwrap(client.post(`/jobs/${jobId}/cancel`));
}

export async function getGroupJob(groupJobId: string): Promise<GroupJob> {
  return unwrap(client.get<ApiResponse<GroupJob>>(`/group-jobs/${groupJobId}`));
}

// ── Users ─────────────────────────────────────────────────────────────────────

export async function getUsers(): Promise<User[]> {
  const data = await unwrap<{ items: User[]; total: number; page: number; page_size: number }>(
    client.get<ApiResponse<{ items: User[]; total: number; page: number; page_size: number }>>('/users/'),
  );
  return data.items ?? [];
}

export async function createUser(body: UserCreate) {
  return unwrap(client.post('/users/', body));
}

export async function updateUser(userId: number, body: UserUpdate) {
  return unwrap(client.put(`/users/${userId}`, body));
}

export async function deleteUser(userId: number) {
  return unwrap(client.delete(`/users/${userId}`));
}

// ── Audit ─────────────────────────────────────────────────────────────────────

export async function getAuditLogs(params?: {
  user?: string;
  action?: string;
  resource?: string;
  status?: string;
  device_id?: string;
  site_id?: number;
  from_date?: string;
  to_date?: string;
  page?: number;
  page_size?: number;
  skip?: number;
  limit?: number;
}): Promise<{ items: AuditLog[]; total: number }> {
  const data = await unwrap<{ items: AuditLog[]; total: number; page: number; page_size: number }>(
    client.get<ApiResponse<{ items: AuditLog[]; total: number; page: number; page_size: number }>>('/audit/', { params }),
  );
  return { items: data.items ?? [], total: data.total ?? 0 };
}

// ── Device Groups ─────────────────────────────────────────────────────────────

export interface DeviceGroup {
  id: number;
  name: string;
  description: string | null;
  member_count: number;
  created_at: string;
  site_id: number | null;
  site_name: string | null;
}

export async function getDeviceGroups(): Promise<DeviceGroup[]> {
  return unwrap(client.get<ApiResponse<DeviceGroup[]>>('/device-groups/'));
}

export async function createDeviceGroup(body: {
  name: string;
  description?: string;
  site_id: number;
}): Promise<DeviceGroup> {
  return unwrap(client.post<ApiResponse<DeviceGroup>>('/device-groups/', body));
}

export async function deleteDeviceGroup(groupId: number): Promise<void> {
  return unwrap(client.delete(`/device-groups/${groupId}`));
}

export async function getDeviceGroupDevices(groupId: number): Promise<string[]> {
  const { data } = await client.get(`/device-groups/${groupId}/devices`);
  return Array.isArray(data)
    ? data
    : Array.isArray(data.data)
      ? data.data
      : Array.isArray(data.devices)
        ? data.devices
        : Array.isArray(data.data?.devices)
          ? data.data.devices
          : [];
}

// ── Sites ─────────────────────────────────────────────────────────────────────

export async function getSites(): Promise<Site[]> {
  return unwrap(client.get<ApiResponse<Site[]>>('/sites/'));
}

export async function getSite(siteId: number): Promise<Site> {
  return unwrap(client.get<ApiResponse<Site>>(`/sites/${siteId}`));
}

export async function createSite(body: SiteCreate): Promise<Site> {
  return unwrap(client.post<ApiResponse<Site>>('/sites/', body));
}

export async function updateSite(siteId: number, body: SiteUpdate): Promise<Site> {
  return unwrap(client.put<ApiResponse<Site>>(`/sites/${siteId}`, body));
}

export async function deleteSite(siteId: number): Promise<void> {
  return unwrap(client.delete(`/sites/${siteId}`));
}

/**
 * MSP: Phase 4 — new endpoint listing every group that belongs to the
 * given site. Used by the device registration modal to populate the Group
 * dropdown after the caller picks a Site.
 */
export async function listSiteGroups(siteId: number): Promise<DeviceGroup[]> {
  return unwrap(client.get<ApiResponse<DeviceGroup[]>>(`/sites/${siteId}/groups`));
}

// ── Grants / system-admin (MSP Phase 3+) ────────────────────────────────────

export async function listGrants(userId: number): Promise<RoleAssignment[]> {
  return unwrap(
    client.get<ApiResponse<RoleAssignment[]>>(`/users/${userId}/grants`),
  );
}

export async function grant(
  userId: number,
  body: RoleAssignmentCreate,
): Promise<RoleAssignment> {
  return unwrap(
    client.post<ApiResponse<RoleAssignment>>(`/users/${userId}/grants`, body),
  );
}

export async function revoke(userId: number, grantId: number): Promise<void> {
  await client.delete(`/users/${userId}/grants/${grantId}`);
}

/**
 * MSP: Phase 4 — toggle the system-admin flag on a user. Requires the
 * caller to be a system-admin themselves. Blocks demoting the last active
 * system-admin (400).
 */
export async function setSystemAdmin(
  userId: number,
  isSystemAdmin: boolean,
): Promise<{ id: number; is_system_admin: boolean }> {
  return unwrap(
    client.put<ApiResponse<{ id: number; is_system_admin: boolean }>>(
      `/users/${userId}/system-admin`,
      { is_system_admin: isSystemAdmin },
    ),
  );
}
