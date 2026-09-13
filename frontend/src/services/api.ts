import axios from 'axios';
import type { AuthUser } from '@/types/auth';
import { getLastActivity } from '@/lib/sessionActivity';
import type {
  VlanEntry,
  VlanCreate,
  VlanUpdate,
  VlanDelete,
  VlanOperationResult,
  VlanBatchRequest,
} from '@/types/vlan';
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
  PortBatchRequest,
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
  SVIBatchRequest,
  SVICreateRequest,
  SVIDeleteRequest,
  SVIDescriptionClearRequest,
  SVIDescriptionUpdateRequest,
  SVIDhcpRelayAddRequest,
  SVIDhcpRelayRemoveRequest,
  SVIIpv4ClearRequest,
  SVIIpv4SecondaryAddRequest,
  SVIIpv4SecondaryRemoveRequest,
  SVIIpv4UpdateRequest,
  SVIIpv6ClearRequest,
  SVIIpv6UpdateRequest,
  SVIListResponse,
  SVIOperationResult,
} from '@/types/svi';
import type { DashboardSummary, DashboardSummaryParams } from '@/types/dashboard';
import type {
  AclCreateRequest,
  AclDeleteRequest,
  AclRuleRemoveRequest,
  ArpTableRead,
  DeviceLogsRead,
  DnsAddRequest,
  DnsRemoveRequest,
  GlobalConfigOperationResult,
  GlobalConfigRead,
  GlobalConfigRunningConfigRead,
  GlobalConfigVersionRead,
  HostnameUpdateRequest,
  LogServerAddRequest,
  LogServerRemoveRequest,
  MacTableRead,
  NtpAddRequest,
  NtpRemoveRequest,
  RouteAddRequest,
  RouteRemoveRequest,
  SnmpUpdateRequest,
} from '@/types/global-config';

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
const SESSION_EXPIRED_REASON = 'ansiauth.sessionExpiredReason';

// Backend detail codes returned by /auth/refresh on 401 — see
// backend/app/api/auth.py:_REFRESH_ERROR_DETAIL. The login page maps these
// to user-facing copy; anything else falls back to a generic message.
export type SessionExpiredReason =
  | 'idle_timeout'
  | 'session_absolute_limit'
  | 'replay_detected'
  | 'expired'
  | 'invalid';

let _sessionExpiredHandler: (() => void) | null = null;

export function onSessionExpired(handler: () => void): () => void {
  _sessionExpiredHandler = handler;
  return () => {
    if (_sessionExpiredHandler === handler) _sessionExpiredHandler = null;
  };
}

function notifySessionExpired(reason?: SessionExpiredReason | null): void {
  cancelProactiveRefresh();
  _accessToken = null;
  if (typeof window !== 'undefined') {
    try {
      window.sessionStorage.setItem(SESSION_EXPIRED_FLAG, '1');
      if (reason) {
        window.sessionStorage.setItem(SESSION_EXPIRED_REASON, reason);
      } else {
        window.sessionStorage.removeItem(SESSION_EXPIRED_REASON);
      }
    } catch {
      /* ignore quota / disabled storage */
    }
  }
  _sessionExpiredHandler?.();
}

function extractRefreshErrorReason(error: unknown): SessionExpiredReason | null {
  const detail = (error as { response?: { data?: { detail?: unknown; message?: unknown } } })
    ?.response?.data;
  const raw = String(detail?.detail ?? detail?.message ?? '');
  const known: SessionExpiredReason[] = [
    'idle_timeout',
    'session_absolute_limit',
    'replay_detected',
    'expired',
    'invalid',
  ];
  return known.find((code) => raw.includes(code)) ?? null;
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

export function consumeSessionExpiredReason(): SessionExpiredReason | null {
  if (typeof window === 'undefined') return null;
  try {
    const value = window.sessionStorage.getItem(SESSION_EXPIRED_REASON);
    if (value) {
      window.sessionStorage.removeItem(SESSION_EXPIRED_REASON);
      return value as SessionExpiredReason;
    }
  } catch {
    /* ignore */
  }
  return null;
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
// Schedules a refresh shortly before the access token's `exp`. Only fires
// when the user was recently active — otherwise renewing a token while the
// user is away just extends the attack window for someone sitting down at
// the machine. The idle-timeout hook handles the "user is gone" case
// separately (14 min → force logout); this scheduler stays out of its way.

let _refreshTimer: ReturnType<typeof setTimeout> | null = null;

// If the last activity is older than this, skip the proactive refresh.
// The idle hook will fire well before this in normal use; keeping the check
// here anyway protects against timers that survive a component unmount.
const ACTIVITY_STALE_MS = 2 * 60 * 1000;

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
    // Skip renewal if the user has not touched anything recently — a
    // silent refresh under an unattended tab is exactly the "make my
    // stolen session last longer" behaviour we're closing.
    if (Date.now() - getLastActivity() > ACTIVITY_STALE_MS) {
      return;
    }
    refreshAccessToken().catch((err) => {
      notifySessionExpired(extractRefreshErrorReason(err));
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

  refreshAccessToken().catch((err) => {
    notifySessionExpired(extractRefreshErrorReason(err));
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
    } catch (refreshErr) {
      notifySessionExpired(extractRefreshErrorReason(refreshErr));
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

/** Pydantic validation errors (`RequestValidationError`, see
 * `request_validation_error_handler` in `backend/app/main.py`) come back
 * as `details.errors: [{loc: ["body", "ipv4_address"], msg: "..."}]` --
 * `extractMessage()` above only ever surfaces the FIRST one as a single
 * generic string, with no link back to which field it's about, so a form
 * with several inputs (e.g. SVIEditModal's IPv4/IPv6/ACL tabs) had no way
 * to show the real reason next to the field the user is actually looking
 * at -- it either showed nothing useful or a bottom-of-modal message the
 * user had to go find in the Audit Logs' raw JSON to actually read.
 *
 * Returns a `{fieldName: message}` map keyed by the full `loc` path (minus
 * the leading `"body"`), dot-joined -- for a flat body this is just the
 * field name (`ipv4_address`, matching this app's convention of naming a
 * form's local state after the wire field name), but for a field nested
 * inside a list (e.g. GlobalConfigAcl's `rules: [...]`,
 * `["body","rules",2,"protocol"]`) it's `"rules.2.protocol"` -- keeping the
 * index means an error on rule 2 doesn't collide with the same field name
 * on rule 0. Multiple errors on the same field are joined with '; '.
 * Returns `null` for any error shape that isn't this validation response
 * (network error, a different 4xx/5xx, etc.) so callers can tell "no
 * field-level detail available" apart from "no errors at all". */
export function parseFieldErrors(error: unknown): Record<string, string> | null {
  const e = error as {
    response?: { data?: { details?: { errors?: Array<{ loc?: unknown[]; msg?: string }> } } };
  } | null;
  const errors = e?.response?.data?.details?.errors;
  if (!errors || errors.length === 0) return null;
  const fields: Record<string, string> = {};
  for (const err of errors) {
    const loc = Array.isArray(err.loc) ? err.loc : [];
    // Full loc path (minus the leading "body"), dot-joined, not just the
    // last segment -- a flat body (`["body","ipv4_address"]`) still keys
    // as `"ipv4_address"` (unchanged), but a nested/indexed one (a rule
    // inside GlobalConfigAcl's `rules: [...]`, `["body","rules",2,"protocol"]`)
    // keys as `"rules.2.protocol"` instead of colliding with every other
    // rule's `"protocol"` under the same last-segment-only key.
    const segments = loc.filter((s) => s !== 'body').map(String);
    const field = segments.length > 0 ? segments.join('.') : null;
    if (!field || !err.msg) continue;
    fields[field] = fields[field] ? `${fields[field]}; ${err.msg}` : err.msg;
  }
  return Object.keys(fields).length > 0 ? fields : null;
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
  const id = typeof payload.id === 'number' ? payload.id : undefined;
  return {
    id,
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
      window.sessionStorage.removeItem(SESSION_EXPIRED_REASON);
    } catch {
      /* ignore */
    }
  }
}

/**
 * Client-side idle logout. Revokes the refresh cookie server-side (best
 * effort — a network hiccup shouldn't strand the user), then flags the
 * login page to show the "signed out for inactivity" banner via the same
 * mechanism a server-side idle 401 uses.
 */
export async function signOutClientIdle(): Promise<void> {
  await client.post('/auth/logout').catch(() => {});
  notifySessionExpired('idle_timeout');
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

// POST /vlans/batch — one request applies N VLAN operations (create /
// rename / delete, mixed) to M devices. Backend enqueues one job per
// device that runs every operation in a single SSH session, replacing
// the older per-VLAN endpoints where 5 deletions × 2 devices produced 10
// jobs / 10 sessions.
export async function batchVlans(body: VlanBatchRequest): Promise<VlanOperationResult> {
  const result = await unwrap<VlanOperationResult>(
    client.post<ApiResponse<VlanOperationResult>>('/vlans/batch', body),
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

/** POST /devices/{name}/ports/batch — N changes (across ports, fields, or
 * both) applied in 1 SSH connection instead of 1 per change. See
 * PortBatchRequest. */
export async function batchUpdatePorts(
  device: string,
  body: PortBatchRequest,
): Promise<PortOperationResult> {
  const result = await unwrap<PortOperationResult>(
    client.post<ApiResponse<PortOperationResult>>(`/devices/${device}/ports/batch`, body),
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

/** Full envelope variant — mismo criterio que getVlansSynced/getPortsSynced.
 * Preferido para dashboards / tabs que muestran "Last synced Xm ago". */
export async function getSVIsSynced(
  device: string,
): Promise<SyncedResource<SVIListResponse>> {
  return unwrap<SyncedResource<SVIListResponse>>(
    client.get<ApiResponse<SyncedResource<SVIListResponse>>>(`/devices/${device}/svis/`),
  );
}

/** Fires the async Celery refresh for a device's SVI cache. Returns as soon
 * as the task is queued (backend responds 202) — callers poll getSVIsSynced
 * watching `sync_in_progress` to know when the DB is fresh. */
export async function refreshDeviceSvis(
  device: string,
): Promise<{ device: string; scope: 'svis'; task_id: string }> {
  return unwrap<{ device: string; scope: 'svis'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'svis'; task_id: string }>>(
      `/devices/${device}/svis/refresh`,
    ),
  );
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

export async function addSVIIpv4Secondary(
  device: string,
  body: SVIIpv4SecondaryAddRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.post<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv4-secondary`, body),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeSVIIpv4Secondary(
  device: string,
  body: SVIIpv4SecondaryRemoveRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.delete<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/ipv4-secondary`, { data: body }),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

/** PATCH /devices/{name}/svis/{vlan_id}/batch — N field changes on 1 SVI
 * applied in 1 SSH connection instead of 1 per field. See SVIBatchRequest --
 * DHCP relay and secondary-IPv4 add/remove both fold into this same batch,
 * despite the dedicated functions above existing for API parity (neither
 * is actually called by SVIEditModal, which always goes through this one). */
export async function batchUpdateSvi(
  device: string,
  vlanId: number,
  body: SVIBatchRequest,
): Promise<SVIOperationResult> {
  const result = await unwrap<SVIOperationResult>(
    client.patch<ApiResponse<SVIOperationResult>>(`/devices/${device}/svis/${vlanId}/batch`, body),
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

/** Manually re-attempt the rollback of a job whose original rollback
 * failed (rollback_success=false). Creates a NEW job with
 * operation='retry_rollback' that runs the batched revert against the
 * device using the failed job's persisted pre_state snapshot. The
 * original job stays as-is. Only supported for puerto/svi jobs.
 * Returns the new job id + group_job_id for polling. */
export async function retryJobRollback(
  jobId: string,
): Promise<{ job_id: string; group_job_id: string; status: string; retry_of_job_id: string }> {
  return unwrap(client.post(`/jobs/${jobId}/retry-rollback`));
}

export async function getGroupJob(groupJobId: string): Promise<GroupJob> {
  return unwrap(client.get<ApiResponse<GroupJob>>(`/group-jobs/${groupJobId}`));
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

/** Server-side aggregation. Reemplaza el patrón N+1 del dashboard:
 * antes eran (1 + 3N) requests con N = cantidad de devices; ahora es 1.
 * Ver docs/prompts/new/plan-dashboard-summary-endpoint.md */
export async function getDashboardSummary(
  params: DashboardSummaryParams,
): Promise<DashboardSummary> {
  const { includeGlobalConfig, ...rest } = params;
  return unwrap<DashboardSummary>(
    client.get<ApiResponse<DashboardSummary>>('/dashboard/summary', {
      params: { ...rest, include_global_config: includeGlobalConfig || undefined },
    }),
  );
}

export interface DashboardRefreshResult {
  /** Devices whose sync_device_task was actually enqueued. */
  devices_queued: number;
  /** Devices considered fresh (skipped by staleness filter). */
  devices_skipped_fresh?: number;
  /** Devices that already had a pending sync (skipped by coalescing). */
  devices_skipped_coalesced?: number;
  /** Same as devices_queued -- kept for backwards compatibility. */
  tasks_dispatched: number;
  /** Only present when the request set includeGlobalConfig. */
  global_config_devices_queued?: number;
  global_config_devices_skipped_fresh?: number;
  global_config_devices_skipped_coalesced?: number;
}

/** Reactive-refresh endpoint: pide al backend que sincronice sólo los
 * devices "stale" del scope (default umbral 7 min), con coalescing para
 * no re-encolar los que ya tienen una sync pending. Fire and forget: la
 * UI simplemente hace polling del summary hasta que
 * ``sync_in_progress_count`` vuelve a 0. Reemplaza al viejo botón manual
 * de refresh global -- el barrido periódico completo lo hace ahora Celery
 * Beat (``sync_stale_devices_task``). */
export type DashboardRefreshParams = Pick<
  DashboardSummaryParams,
  'scope' | 'id' | 'name' | 'includeGlobalConfig'
>;

export async function refreshDashboardScope(
  params: DashboardRefreshParams,
): Promise<DashboardRefreshResult> {
  const { includeGlobalConfig, ...rest } = params;
  return unwrap<DashboardRefreshResult>(
    client.post<ApiResponse<DashboardRefreshResult>>(
      '/dashboard/refresh',
      undefined,
      { params: { ...rest, include_global_config: includeGlobalConfig || undefined } },
    ),
  );
}

// ── Users ─────────────────────────────────────────────────────────────────────

export async function getUsers(): Promise<User[]> {
  const data = await unwrap<{ items: User[]; total: number; page: number; page_size: number }>(
    client.get<ApiResponse<{ items: User[]; total: number; page: number; page_size: number }>>('/users/'),
  );
  return data.items ?? [];
}

export async function createUser(body: UserCreate): Promise<User> {
  return unwrap(client.post<ApiResponse<User>>('/users/', body));
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

// ── Global Configuration ─────────────────────────────────────────────────────
// Same cache-first / SyncedResource pattern as VLANs/Ports/SVIs. Writes are
// async (202) and return the same group_job_id + jobs[] envelope the rest of
// the app already knows how to poll.

/** GET /devices/{name}/global-config/ — hostname / SNMP / NTP / DNS / logging
 * / routes / ACLs. Cache-first; no live device read. */
export async function getGlobalConfigSynced(
  device: string,
): Promise<SyncedResource<GlobalConfigRead>> {
  return unwrap<SyncedResource<GlobalConfigRead>>(
    client.get<ApiResponse<SyncedResource<GlobalConfigRead>>>(
      `/devices/${device}/global-config/`,
    ),
  );
}

/** GET /devices/{name}/global-config/version — software_version / model /
 * uptime. Shares the same underlying `global_config` sync as the main GET. */
export async function getGlobalConfigVersionSynced(
  device: string,
): Promise<SyncedResource<GlobalConfigVersionRead>> {
  return unwrap<SyncedResource<GlobalConfigVersionRead>>(
    client.get<ApiResponse<SyncedResource<GlobalConfigVersionRead>>>(
      `/devices/${device}/global-config/version`,
    ),
  );
}

/** GET /devices/{name}/global-config/running-config — full config dump as
 * a list of lines. Shares the `global_config` sync scope. */
export async function getGlobalConfigRunningConfigSynced(
  device: string,
): Promise<SyncedResource<GlobalConfigRunningConfigRead>> {
  return unwrap<SyncedResource<GlobalConfigRunningConfigRead>>(
    client.get<ApiResponse<SyncedResource<GlobalConfigRunningConfigRead>>>(
      `/devices/${device}/global-config/running-config`,
    ),
  );
}

/** GET /devices/{name}/global-config/arp — cache-first. Has its OWN sync
 * scope (`arp_mac`), separate from the general refresh. Not populated on
 * device registration; the user has to call the arp-mac refresh at least
 * once for entries to show up. `include` filters cached rows server-side
 * (case-insensitive substring across all fields). */
export async function getArpTable(
  device: string,
  include?: string,
): Promise<SyncedResource<ArpTableRead>> {
  return unwrap<SyncedResource<ArpTableRead>>(
    client.get<ApiResponse<SyncedResource<ArpTableRead>>>(
      `/devices/${device}/global-config/arp`,
      { params: include ? { include } : {} },
    ),
  );
}

/** GET /devices/{name}/global-config/mac — same `arp_mac` sync scope as
 * getArpTable(). */
export async function getMacTable(
  device: string,
  include?: string,
): Promise<SyncedResource<MacTableRead>> {
  return unwrap<SyncedResource<MacTableRead>>(
    client.get<ApiResponse<SyncedResource<MacTableRead>>>(
      `/devices/${device}/global-config/mac`,
      { params: include ? { include } : {} },
    ),
  );
}

/** GET /devices/{name}/global-config/logs — local log buffer as a list of
 * lines. Has its own `logs` sync scope, same criterion as ARP/MAC — has to
 * be refreshed explicitly the first time. */
export async function getDeviceLogsSynced(
  device: string,
): Promise<SyncedResource<DeviceLogsRead>> {
  return unwrap<SyncedResource<DeviceLogsRead>>(
    client.get<ApiResponse<SyncedResource<DeviceLogsRead>>>(
      `/devices/${device}/global-config/logs`,
    ),
  );
}

/** POST /devices/{name}/global-config/refresh — queues the general
 * global-config sync (hostname / snmp / ntp / dns / logging / routes / acls /
 * version / running-config, all one scope). ARP/MAC and logs have their own
 * refresh endpoints below. */
export async function refreshDeviceGlobalConfig(
  device: string,
): Promise<{ device: string; scope: 'global_config'; task_id: string }> {
  return unwrap<{ device: string; scope: 'global_config'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'global_config'; task_id: string }>>(
      `/devices/${device}/global-config/refresh`,
    ),
  );
}

/** POST /devices/{name}/global-config/arp-mac/refresh — separate sync
 * scope on purpose (ARP/MAC can be huge and aren't needed for any write's
 * no-op check). Poll getArpTable/getMacTable for `sync_in_progress`. */
export async function refreshDeviceArpMac(
  device: string,
): Promise<{ device: string; scope: 'arp_mac'; task_id: string }> {
  return unwrap<{ device: string; scope: 'arp_mac'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'arp_mac'; task_id: string }>>(
      `/devices/${device}/global-config/arp-mac/refresh`,
    ),
  );
}

/** POST /devices/{name}/global-config/logs/refresh — separate `logs` sync
 * scope, same criterion as ARP/MAC. */
export async function refreshDeviceLogs(
  device: string,
): Promise<{ device: string; scope: 'logs'; task_id: string }> {
  return unwrap<{ device: string; scope: 'logs'; task_id: string }>(
    client.post<ApiResponse<{ device: string; scope: 'logs'; task_id: string }>>(
      `/devices/${device}/global-config/logs/refresh`,
    ),
  );
}

// ── Global Config writes (all 202, admin role required) ──────────────────────

export async function setGlobalConfigHostname(
  device: string,
  body: HostnameUpdateRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.patch<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/hostname`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function setGlobalConfigSnmp(
  device: string,
  body: SnmpUpdateRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.patch<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/snmp`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function addGlobalConfigRoute(
  device: string,
  body: RouteAddRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.post<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/routes`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeGlobalConfigRoute(
  device: string,
  body: RouteRemoveRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/routes`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function addGlobalConfigNtp(
  device: string,
  body: NtpAddRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.post<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/ntp`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeGlobalConfigNtp(
  device: string,
  body: NtpRemoveRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/ntp`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function addGlobalConfigDns(
  device: string,
  body: DnsAddRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.post<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/dns`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeGlobalConfigDns(
  device: string,
  body: DnsRemoveRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/dns`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function addGlobalConfigLogServer(
  device: string,
  body: LogServerAddRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.post<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/log-servers`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeGlobalConfigLogServer(
  device: string,
  body: LogServerRemoveRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/log-servers`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function createOrUpdateGlobalConfigAcl(
  device: string,
  body: AclCreateRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.post<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/acls`,
      body,
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function removeGlobalConfigAclRules(
  device: string,
  body: AclRuleRemoveRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/acls/rules`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}

export async function deleteGlobalConfigAcl(
  device: string,
  body: AclDeleteRequest,
): Promise<GlobalConfigOperationResult> {
  const result = await unwrap<GlobalConfigOperationResult>(
    client.delete<ApiResponse<GlobalConfigOperationResult>>(
      `/devices/${device}/global-config/acls`,
      { data: body },
    ),
  );
  return { group_job_id: result.group_job_id, jobs: result.jobs ?? [] };
}
