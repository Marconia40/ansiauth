export type Vendor = 'cisco_ios' | 'huawei_vrp';

export type AuthMethod = 'password' | 'key';

/** Display label for a vendor value returned by the API. */
export function vendorLabel(vendor: Vendor | string): string {
  if (vendor === 'cisco_ios') return 'Cisco';
  if (vendor === 'huawei_vrp') return 'Huawei';
  return vendor;
}

/**
 * Matches backend DevicePublic schema post-Phase 4.
 *
 * MSP: Phase 4 flipped `site_id`, `site_name`, `device_group_id`, and
 * `device_group_name` to always-populated fields on every response. The
 * `id` field is coerced to string by Pydantic.
 */
export interface Device {
  id: string;
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
  auth_method: AuthMethod;
  site_id: number;
  site_name: string;
  device_group_id: number;
  device_group_name: string;
}

/**
 * Matches backend DeviceCreate post-Phase 4.
 *
 * `site_id` is required. `device_group_id` is optional — when omitted the
 * device lands in that site's Default group. The backend rejects a
 * mismatch between the provided group's site_id and the body's site_id.
 *
 * Exactly one of `password`/`private_key` must be set, matching
 * `auth_method` — the backend's `_validar_credencial` enforces this
 * server-side too.
 */
export interface DeviceCreate {
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
  auth_method: AuthMethod;
  password?: string;
  private_key?: string;
  site_id: number;
  device_group_id?: number;
}

/**
 * Matches backend DeviceUpdate post-Phase 4.
 *
 * `site_id` and `device_group_id` are NOT accepted here — callers must
 * use `POST /devices/{name}/move` (see `moveDevice` in the API client)
 * to change a device's group or site. Any extra key raises a 422.
 *
 * `password`/`private_key` are optional here (unlike create) — omit both
 * to leave the stored secret unchanged. Setting one without `auth_method`
 * infers it server-side; setting both in the same call is rejected.
 */
export interface DeviceUpdate {
  host?: string;
  vendor?: string;
  platform?: string;
  username?: string;
  auth_method?: AuthMethod;
  password?: string;
  private_key?: string;
}

/**
 * Body for `POST /devices/{name}/move`.
 *
 * D8: pass `device_group_id: null` (or omit) to move the device to its
 * current site's Default group. Otherwise pass the target group ID; the
 * backend picks same-site vs cross-site authz based on the group's site.
 */
export interface DeviceMove {
  device_group_id: number | null;
}
