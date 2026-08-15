export type Vendor = 'cisco' | 'huawei';

// Matches backend DevicePublic schema (id is coerced to string by Pydantic)
export interface Device {
  id: string;
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
}

// Matches backend DeviceCreate schema
export interface DeviceCreate {
  name: string;
  host: string;
  vendor: Vendor;
  platform: string;
  username: string;
  password: string;
}

// Matches backend DeviceUpdate schema (all fields optional)
export interface DeviceUpdate {
  host?: string;
  vendor?: string;
  platform?: string;
  username?: string;
  password?: string;
}
