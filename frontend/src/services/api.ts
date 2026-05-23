import axios from 'axios';
import type { AuthUser } from '@/types/auth';
import type { VlanEntry, VlanCreate, VlanUpdate, VlanDelete, VlanJobResult } from '@/types/vlan';
import type { Device, DeviceCreate, DeviceUpdate } from '@/types/device';
import type { User, UserCreate, UserUpdate } from '@/types/user';
import type { Job } from '@/types/job';
import type { AuditLog } from '@/types/audit';

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
}

export function clearAccessToken(): void {
  _accessToken = null;
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

// ── Refresh token interceptor ─────────────────────────────────────────────────

let _isRefreshing = false;
let _refreshQueue: Array<(token: string) => void> = [];

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;

    if (error.response?.status !== 401 || original._retry) {
      return Promise.reject(error);
    }

    original._retry = true;

    if (_isRefreshing) {
      return new Promise((resolve) => {
        _refreshQueue.push((token) => {
          original.headers.Authorization = `Bearer ${token}`;
          resolve(client(original));
        });
      });
    }

    _isRefreshing = true;

    try {
      const { data } = await axios.post<{ access_token: string }>(
        `${BASE_URL}/api/v1/auth/refresh`,
        undefined,
        { withCredentials: true },
      );

      _accessToken = data.access_token;

      _refreshQueue.forEach((cb) => cb(data.access_token));
      _refreshQueue = [];

      original.headers.Authorization = `Bearer ${data.access_token}`;

      return client(original);
    } catch {
      clearAccessToken();

      if (typeof window !== 'undefined') {
        window.location.href = '/login';
      }

      return Promise.reject(error);
    } finally {
      _isRefreshing = false;
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
    const { data } = await axios.post<{ access_token: string }>(
      `${BASE_URL}/api/v1/auth/refresh`,
      undefined,
      { withCredentials: true },
    );
    setAccessToken(data.access_token);
    const payload = JSON.parse(atob(data.access_token.split('.')[1]));
    return { username: payload.sub, role: payload.role };
  } catch {
    return null;
  }
}

export async function logout(): Promise<void> {
  await client.post('/auth/logout').catch(() => {});
  clearAccessToken();
}

// ── VLANs ─────────────────────────────────────────────────────────────────────

export async function getVlans(device?: string): Promise<VlanEntry[]> {
  return unwrap(
    client.get<ApiResponse<VlanEntry[]>>('/vlans/', {
      params: device ? { device } : {},
    }),
  );
}

export async function createVlan(body: VlanCreate): Promise<VlanJobResult[]> {
  const { data } = await client.post<{ success: boolean; jobs: VlanJobResult[] }>('/vlans/', body);
  return data.jobs ?? [];
}

export async function updateVlan(vlanId: number, body: VlanUpdate): Promise<VlanJobResult[]> {
  const { data } = await client.patch<{ success: boolean; jobs: VlanJobResult[] }>(`/vlans/${vlanId}`, body);
  return data.jobs ?? [];
}

export async function deleteVlan(vlanId: number, body: VlanDelete): Promise<VlanJobResult[]> {
  const { data } = await client.delete<{ success: boolean; jobs: VlanJobResult[] }>(`/vlans/${vlanId}`, {
    data: body,
  });
  return data.jobs ?? [];
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
}) {
  const { data } = await client.get<{ items: unknown[]; total: number }>('/jobs/', { params });
  return data.items ?? [];
}

export async function getJob(jobId: string): Promise<Job> {
  return unwrap(client.get<ApiResponse<Job>>(`/jobs/${jobId}`));
}

export async function cancelJob(jobId: string) {
  return unwrap(client.post(`/jobs/${jobId}/cancel`));
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
  skip?: number;
  limit?: number;
}): Promise<AuditLog[]> {
  const { data } = await client.get<AuditLog[]>('/audit/', { params });
  return Array.isArray(data) ? data : [];
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
