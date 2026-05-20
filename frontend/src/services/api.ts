import axios from 'axios';
import type { TokenResponse, AuthUser } from '@/types/auth';
import type { VlanCreate, VlanUpdate, VlanDelete } from '@/types/vlan';
import type { DeviceCreate, DeviceUpdate } from '@/types/device';
import type { UserCreate, UserUpdate } from '@/types/user';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

// ── API Response Wrapper ──────────────────────────────────────────────────────

type ApiResponse<T> = {
  success: boolean;
  data: T;
};

// ── Token storage ─────────────────────────────────────────────────────────────

let _accessToken: string | null = null;
let _refreshToken: string | null = null;

export function setTokens(access: string, refresh: string): void {
  _accessToken = access;
  _refreshToken = refresh;
}

export function clearTokens(): void {
  _accessToken = null;
  _refreshToken = null;
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

    if (error.response?.status !== 401 || original._retry || !_refreshToken) {
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
      const { data } = await axios.post<TokenResponse>(
        `${BASE_URL}/api/v1/auth/refresh`,
        { refresh_token: _refreshToken },
      );

      _accessToken = data.access_token;
      _refreshToken = data.refresh_token;

      _refreshQueue.forEach((cb) => cb(data.access_token));
      _refreshQueue = [];

      original.headers.Authorization = `Bearer ${data.access_token}`;

      return client(original);
    } catch {
      clearTokens();

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

  const { data } = await axios.post<TokenResponse>(
    `${BASE_URL}/api/v1/auth/login`,
    form,
    {
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
    },
  );

  setTokens(data.access_token, data.refresh_token);

  const payload = JSON.parse(atob(data.access_token.split('.')[1]));

  return {
    username: payload.sub,
    role: payload.role,
  };
}

export async function logout(): Promise<void> {
  if (_refreshToken) {
    await client
      .post('/auth/logout', {
        refresh_token: _refreshToken,
      })
      .catch(() => {});
  }

  clearTokens();

  if (typeof window !== 'undefined') {
    const secure =
      window.location.protocol === 'https:' ? '; Secure' : '';

    document.cookie =
      `session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Strict${secure}`;
  }
}

// ── VLANs ─────────────────────────────────────────────────────────────────────

export async function getVlans(device?: string) {
  return unwrap(
    client.get('/vlans/', {
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

export async function deleteVlan(vlanId: number, body: VlanDelete) {
  return unwrap(
    client.delete(`/vlans/${vlanId}`, {
      data: body,
    }),
  );
}

// ── Devices ───────────────────────────────────────────────────────────────────

export async function getDevices() {
  return unwrap(client.get('/devices/'));
}

export async function getDevice(name: string) {
  return unwrap(client.get(`/devices/${name}`));
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

export async function getJob(jobId: string) {
  return unwrap(client.get(`/jobs/${jobId}`));
}

export async function cancelJob(jobId: string) {
  return unwrap(client.post(`/jobs/${jobId}/cancel`));
}

// ── Users ─────────────────────────────────────────────────────────────────────

export async function getUsers() {
  return unwrap(client.get('/users/'));
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
}) {
  return unwrap(
    client.get('/audit/', {
      params,
    }),
  );
}

// ── Device Groups ─────────────────────────────────────────────────────────────

export async function getDeviceGroups() {
  return unwrap(client.get('/device-groups/'));
}

export async function createDeviceGroup(body: {
  name: string;
  description?: string;
}) {
  return unwrap(client.post('/device-groups/', body));
}

export async function deleteDeviceGroup(name: string) {
  return unwrap(client.delete(`/device-groups/${name}`));
}
