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

// One entry of `POST /vlans/batch` — either a create/rename (`name` set)
// or a delete (`eliminar: true`). Same shape enforced by
// VLANBatchChangeItem on the backend.
export interface VlanBatchChange {
  vlan_id: number;
  name?: string;
  eliminar?: boolean;
}

// Body of `POST /vlans/batch` — N VLAN operations applied to M devices.
// Each device gets 1 job carrying all N ops (1 SSH session per device),
// not 1 job per (device, VLAN) pair. Same group_job_id shared across
// every device so the UI tracks the whole batch as one operation.
export interface VlanBatchRequest {
  changes: VlanBatchChange[];
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

// Aggregate result returned by multi-device VLAN operations
export interface VlanOperationResult {
  group_job_id: string;
  jobs: VlanJobResult[];
}
