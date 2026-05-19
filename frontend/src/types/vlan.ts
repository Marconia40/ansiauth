// Matches backend VLANCreate schema
export interface VlanCreate {
  vlan_id: number;
  name: string;
  devices: string[];
}

// Matches backend VLANUpdate schema
export interface VlanUpdate {
  description: string;
  devices: string[];
}

// Matches backend VLANDelete schema
export interface VlanDelete {
  devices: string[];
}

// A single VLAN entry as returned from the device
export interface VlanEntry {
  vlan_id: number;
  name: string;
  description?: string;
}

// Returned by create/update/delete operations (async job per device)
export interface VlanJobResult {
  device: string;
  job_id: string;
}
