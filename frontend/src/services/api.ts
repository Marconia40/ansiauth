import axios from 'axios';
import type { TokenResponse, AuthUser } from '@/types/auth';
import type { VlanCreate, VlanUpdate, VlanDelete } from '@/types/vlan';
import type { DeviceCreate, DeviceUpdate } from '@/types/device';
import type { UserCreate, UserUpdate } from '@/types/user';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

// ── Token storage ─────────────────────────────────────────────────────────────
// Both tokens are stored in memory only — they are cleared on page refresh.
// This is intentional: it prevents XSS from stealing tokens from storage APIs.
// IMPORTANT: to avoid logout on refresh, the backend must be updated to send
// the refresh token as an httpOnly Set-Cookie header instead of the JSON body.
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
  withCredentials: true, // ready for future httpOnly cookie support
});

// Attach the access token to every request.
client.interceptors.request.use((config) => {
  if (_accessToken) {
    config.headers.Authorization = `Bearer ${_accessToken}`;
  }
  return config;
});

// On 401, attempt a single token refresh and retry the original request.
// Multiple concurrent 401s queue up and wait for the single refresh call.
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
      if (typeof window !== 'undefined') window.location.href = '/login';
      return Promise.reject(error);
    } finally {
      _isRefreshing = false;
    }
  },
);

// ── Auth ──────────────────────────────────────────────────────────────────────

// Login uses OAuth2PasswordRequestForm (form-encoded, not JSON).
export async function login(username: string, password: string): Promise<AuthUser> {
  const form = new URLSearchParams({ username, password });
  const { data } = await axios.post<TokenResponse>(
    `${BASE_URL}/api/v1/auth/login`,
    form,
    { headers: { 'Content-Type': 'application/x-www-form-urlencoded' } },
  );

  setTokens(data.access_token, data.refresh_token);

  // Decode JWT payload (base64url) to extract username and role.
  // The payload is not verified here — the backend validates it on every request.
  const payload = JSON.parse(atob(data.access_token.split('.')[1]));
  return { username: payload.sub, role: payload.role };
}

export async function logout(): Promise<void> {
  if (_refreshToken) {
    await client.post('/auth/logout', { refresh_token: _refreshToken }).catch(() => {});
  }
  clearTokens();
  // Clear the session flag so the proxy redirects to /login on next navigation.
  if (typeof window !== 'undefined') {
    const secure = window.location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = `session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Strict${secure}`;
  }
}

// ── VLANs ─────────────────────────────────────────────────────────────────────

export async function getVlans(device?: string) {
  const { data } = await client.get('/vlans/', { params: device ? { device } : {} });
  return data;
}

export async function createVlan(body: VlanCreate) {
  const { data } = await client.post('/vlans/', body);
  return data;
}

export async function updateVlan(vlanId: number, body: VlanUpdate) {
  const { data } = await client.patch(`/vlans/${vlanId}`, body);
  return data;
}

export async function deleteVlan(vlanId: number, body: VlanDelete) {
  const { data } = await client.delete(`/vlans/${vlanId}`, { data: body });
  return data;
}

// ── Devices ───────────────────────────────────────────────────────────────────

export async function getDevices() {
  const { data } = await client.get('/devices/');
  return data;
}

export async function getDevice(name: string) {
  const { data } = await client.get(`/devices/${name}`);
  return data;
}

export async function createDevice(body: DeviceCreate) {
  const { data } = await client.post('/devices/', body);
  return data;
}

export async function updateDevice(name: string, body: DeviceUpdate) {
  const { data } = await client.put(`/devices/${name}`, body);
  return data;
}

export async function deleteDevice(name: string) {
  const { data } = await client.delete(`/devices/${name}`);
  return data;
}

// ── Jobs ──────────────────────────────────────────────────────────────────────

export async function getJobs(params?: {
  status?: string;
  device?: string;
  page?: number;
  page_size?: number;
}) {
  const { data } = await client.get('/jobs/', { params });
  return data;
}

export async function getJob(jobId: string) {
  const { data } = await client.get(`/jobs/${jobId}`);
  return data;
}

export async function cancelJob(jobId: string) {
  const { data } = await client.post(`/jobs/${jobId}/cancel`);
  return data;
}

// ── Users ─────────────────────────────────────────────────────────────────────

export async function getUsers() {
  const { data } = await client.get('/users/');
  return data;
}

export async function createUser(body: UserCreate) {
  const { data } = await client.post('/users/', body);
  return data;
}

export async function updateUser(userId: number, body: UserUpdate) {
  const { data } = await client.put(`/users/${userId}`, body);
  return data;
}

export async function deleteUser(userId: number) {
  const { data } = await client.delete(`/users/${userId}`);
  return data;
}

// ── Audit ─────────────────────────────────────────────────────────────────────

export async function getAuditLogs(params?: {
  user?: string;
  action?: string;
  skip?: number;
  limit?: number;
}) {
  const { data } = await client.get('/audit/', { params });
  return data;
}

// ── Device Groups ─────────────────────────────────────────────────────────────

export async function getDeviceGroups() {
  const { data } = await client.get('/device-groups/');
  return data;
}

export async function createDeviceGroup(body: { name: string; description?: string }) {
  const { data } = await client.post('/device-groups/', body);
  return data;
}

export async function deleteDeviceGroup(name: string) {
  const { data } = await client.delete(`/device-groups/${name}`);
  return data;
}
