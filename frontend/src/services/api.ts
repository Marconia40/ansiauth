import axios from 'axios';
import type { AuthUser } from '@/types/auth';
import type { VlanEntry, VlanCreate, VlanUpdate, VlanDelete, VlanOperationResult } from '@/types/vlan';
import type { Device, DeviceCreate, DeviceUpdate } from '@/types/device';
import type { User, UserCreate, UserUpdate } from '@/types/user';
import type { Job, GroupJob } from '@/types/job';
import type { AuditLog } from '@/types/audit';
import type { Site, SiteCreate, SiteUpdate } from '@/types/site';

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

  const payload = JSON.parse(atob(data.access_token.split('.')[1]));

  return {
    username: payload.sub,
    role: payload.role,
  };
}

export async function restoreSession(): Promise<AuthUser | null> {
  try {
    const token = await refreshAccessToken();
    const payload = JSON.parse(atob(token.split('.')[1]));
    return { username: payload.sub, role: payload.role };
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

export async function getVlans(device?: string): Promise<VlanEntry[]> {
  return unwrap(
    client.get<ApiResponse<VlanEntry[]>>('/vlans/', {
      params: device ? { device } : {},
    }),
  );
}

type VlanRawResponse = { success: boolean; group_job_id: string; jobs: { device: string; job_id: string }[] };

export async function createVlan(body: VlanCreate): Promise<VlanOperationResult> {
  const { data } = await client.post<VlanRawResponse>('/vlans/', body);
  return { group_job_id: data.group_job_id, jobs: data.jobs ?? [] };
}

export async function updateVlan(vlanId: number, body: VlanUpdate): Promise<VlanOperationResult> {
  const { data } = await client.patch<VlanRawResponse>(`/vlans/${vlanId}`, body);
  return { group_job_id: data.group_job_id, jobs: data.jobs ?? [] };
}

export async function deleteVlan(vlanId: number, body: VlanDelete): Promise<VlanOperationResult> {
  const { data } = await client.delete<VlanRawResponse>(`/vlans/${vlanId}`, { data: body });
  return { group_job_id: data.group_job_id, jobs: data.jobs ?? [] };
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

// ── Jobs ──────────────────────────────────────────────────────────────────────

export async function getJobs(params?: {
  status?: string;
  device?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: Job[]; total: number }> {
  const { data } = await client.get<{ items: Job[]; total: number }>('/jobs/', { params });
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
  return unwrap(client.get<ApiResponse<User[]>>('/users/'));
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
  from_date?: string;
  to_date?: string;
  page?: number;
  page_size?: number;
  skip?: number;
  limit?: number;
}): Promise<{ items: AuditLog[]; total: number }> {
  const response = await client.get<AuditLog[]>('/audit/', { params });
  const items = Array.isArray(response.data) ? response.data : [];
  const header = response.headers?.['x-total-count'] ?? response.headers?.['X-Total-Count'];
  const total = header != null ? Number(header) : items.length;
  return { items, total: Number.isFinite(total) ? total : items.length };
}

// ── Device Groups ─────────────────────────────────────────────────────────────

export interface DeviceGroup {
  id: number;
  name: string;
  description: string | null;
  member_count: number;
  created_at: string;
}

export async function getDeviceGroups(): Promise<DeviceGroup[]> {
  return unwrap(client.get<ApiResponse<DeviceGroup[]>>('/device-groups/'));
}

export async function createDeviceGroup(body: {
  name: string;
  description?: string;
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

export async function addDeviceToGroup(groupId: number, deviceName: string): Promise<unknown> {
  return unwrap(client.post(`/device-groups/${groupId}/members`, { device_name: deviceName }));
}

export async function removeDeviceFromGroup(groupId: number, deviceName: string): Promise<void> {
  return unwrap(client.delete(`/device-groups/${groupId}/members/${deviceName}`));
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
