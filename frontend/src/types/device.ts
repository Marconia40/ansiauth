export type Vendor = 'cisco' | 'huawei';

// Matches backend DevicePublic schema (id is coerced to string by Pydantic)
export interface Device {
  id: string;
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
  site_id: number | null;
  site_name: string | null;
}

// Matches backend DeviceCreate schema
export interface DeviceCreate {
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
  password: string;
  site_id?: number | null;
}

// Matches backend DeviceUpdate schema (all fields optional).
// `site_id: null` explicitly clears the assignment; omitting it leaves it unchanged.
export interface DeviceUpdate {
  host?: string;
  vendor?: string;
  platform?: string;
  username?: string;
  password?: string;
  site_id?: number | null;
}
