// Shared types for the Ports page and its components. The "editor bundle"
// shapes here are the contract between the page (which owns the React state
// and async handlers) and the per-cell renderer components — bundling them
// keeps PortRow's prop list manageable without resorting to a context.

import type { Port, PortMode, TrunkVlanMode } from '@/types/port';

export type AdminFilter = '' | 'enabled' | 'disabled';
export type OperFilter = '' | 'up' | 'down';
export type SortDir = 'asc' | 'desc';

// Most inline editors expose a single "current error" tagged with the port
// it refers to, so a stale error from one row never leaks into another.
export type PortError = { port: string; message: string } | null;


export interface DescriptionEditor {
  editingPort: string | null;
  editingValue: string;
  setEditingValue: (v: string) => void;
  savingPort: string | null;
  errorPort: PortError;
  start: (port: Port) => void;
  save: (port: Port) => void | Promise<void>;
  cancel: () => void;
}

export interface AdminToggler {
  togglingPort: string | null;
  errorPort: PortError;
  confirmingPort: string | null;
  setConfirmingPort: (port: string | null) => void;
  toggle: (port: Port) => void | Promise<void>;
}

export interface AccessVlanEditor {
  editingPort: string | null;
  value: string;
  setValue: (v: string) => void;
  savingPort: string | null;
  errorPort: PortError;
  start: (port: Port) => void;
  save: (port: Port) => void | Promise<void>;
  cancel: () => void;
}

export interface TrunkVlansEditor {
  editingPort: string | null;
  value: string;
  setValue: (v: string) => void;
  mode: TrunkVlanMode;
  setMode: (m: TrunkVlanMode) => void;
  setError: (e: PortError) => void;
  savingPort: string | null;
  errorPort: PortError;
  start: (port: Port) => void;
  save: (port: Port) => void | Promise<void>;
  cancel: () => void;
}

export interface ModeEditor {
  editingPort: string | null;
  mode: 'access' | 'trunk';
  setMode: (m: 'access' | 'trunk') => void;
  accessVlan: string;
  setAccessVlan: (v: string) => void;
  trunkVlans: string;
  setTrunkVlans: (v: string) => void;
  vlanOp: TrunkVlanMode;
  setVlanOp: (op: TrunkVlanMode) => void;
  setError: (e: PortError) => void;
  savingPort: string | null;
  errorPort: PortError;
  start: (port: Port) => void;
  save: (port: Port) => void | Promise<void>;
  cancel: () => void;
}

export interface BulkOps {
  selectedPorts: Set<string>;
  setSelectedPorts: (next: Set<string>) => void;
  executing: boolean;
  errors: string[];
  setErrors: (errs: string[]) => void;
  vlanInput: string;
  setVlanInput: (v: string) => void;
  vlanExpanded: boolean;
  setVlanExpanded: (b: boolean) => void;
  confirmingDisable: boolean;
  setConfirmingDisable: (b: boolean) => void;
  confirmingClearDesc: boolean;
  setConfirmingClearDesc: (b: boolean) => void;
  hasNonAccessSelected: boolean;
  runAction: (action: 'enable' | 'disable' | 'clear-description') => void | Promise<void>;
  setAccessVlan: () => void | Promise<void>;
}

// Convenience bundle: any state that disables row-level edit buttons because
// some other edit / bulk operation is in progress. Keeps PortRow's prop list
// tighter and prevents accidental drift if a new editor is added later.
export interface EditorsBusy {
  bulkExecuting: boolean;
  anyEditing: boolean;
  anySaving: boolean;
}


export interface FilterBarValues {
  search: string;
  setSearch: (v: string) => void;
  filterMode: PortMode | '';
  setFilterMode: (v: PortMode | '') => void;
  filterAdmin: AdminFilter;
  setFilterAdmin: (v: AdminFilter) => void;
  filterOper: OperFilter;
  setFilterOper: (v: OperFilter) => void;
  pageSize: number;
  setPageSize: (n: number) => void;
  hasActiveFilters: boolean;
  onClear: () => void;
}
