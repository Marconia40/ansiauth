'use client';

import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { configurePort, getDevices, getPorts, getSites, setPortAccessVlan, setTrunkAllowedVlans, updatePortDescription } from '@/services/api';
import type { Device } from '@/types/device';
import type { Site } from '@/types/site';
import type { Port, PortListResponse, PortMode } from '@/types/port';

// ── Helpers ───────────────────────────────────────────────────────────────────

const DASH = '—';
const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

function getStatus(error: unknown): number | null {
  const e = error as { response?: { status?: number } } | null;
  return e?.response?.status ?? null;
}

function getErrorCode(error: unknown): string | null {
  const e = error as { response?: { data?: { error_code?: string } } } | null;
  return e?.response?.data?.error_code ?? null;
}

function isUnsupportedVendorError(error: unknown): boolean {
  return getStatus(error) === 501 || getErrorCode(error) === 'VENDOR_NOT_SUPPORTED';
}

function formatVlanList(vlans: number[] | null): string {
  if (vlans == null) return DASH;
  if (vlans.length === 0) return 'None';
  if (vlans.length === 1) return String(vlans[0]);

  // Collapse consecutive ranges (e.g. [10,11,12,20,21] → "10-12, 20-21")
  const sorted = [...vlans].sort((a, b) => a - b);
  const ranges: string[] = [];
  let start = sorted[0];
  let prev = sorted[0];
  for (let i = 1; i < sorted.length; i++) {
    const v = sorted[i];
    if (v === prev + 1) {
      prev = v;
      continue;
    }
    ranges.push(start === prev ? `${start}` : `${start}-${prev}`);
    start = v;
    prev = v;
  }
  ranges.push(start === prev ? `${start}` : `${start}-${prev}`);

  const text = ranges.join(', ');
  // For huge trunk lists (e.g. 2-4094) truncate visual noise while keeping the count.
  if (vlans.length > 16 && ranges.length === 1) {
    return `${ranges[0]} (${vlans.length})`;
  }
  return text;
}

// ── VLAN input parsing + validation ──────────────────────────────────────────
// Parses user input like "10,20,30-35" or "10 20 30 to 35" into a sorted
// unique number[] and returns either the list or an error string.

const RESERVED_VLANS = new Set([1002, 1003, 1004, 1005]);

function parseVlanInput(raw: string): { vlans: number[]; error: string | null } {
  const tokens = raw.split(/[\s,]+/).filter(Boolean);
  if (tokens.length === 0) return { vlans: [], error: 'Enter at least one VLAN ID' };

  const set = new Set<number>();
  for (const tok of tokens) {
    // Range: "10-20" or "10 to 20"
    const range = tok.match(/^(\d+)(?:-|to)(\d+)$/i);
    if (range) {
      const lo = parseInt(range[1], 10);
      const hi = parseInt(range[2], 10);
      if (lo > hi) return { vlans: [], error: `Invalid range: ${tok} (start must be ≤ end)` };
      for (let v = lo; v <= hi; v++) set.add(v);
      continue;
    }
    // Single ID
    const n = parseInt(tok, 10);
    if (isNaN(n) || String(n) !== tok.trim()) {
      return { vlans: [], error: `'${tok}' is not a valid VLAN ID` };
    }
    set.add(n);
  }

  const vlans = [...set].sort((a, b) => a - b);

  for (const v of vlans) {
    if (v < 1 || v > 4094) return { vlans: [], error: `VLAN ${v} is out of range (1–4094)` };
    if (RESERVED_VLANS.has(v)) return { vlans: [], error: `VLAN ${v} is reserved (Cisco legacy)` };
  }

  return { vlans, error: null };
}


// ── Status pills ──────────────────────────────────────────────────────────────

function RowSpinner() {
  return (
    <div className="inline-block w-3 h-3 animate-spin rounded-full border-2 border-gray-300 border-t-blue-500" />
  );
}

function AdminBadge({ value }: { value: boolean | null }) {
  if (value === null) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-500">
        Unknown
      </span>
    );
  }
  if (value) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
        Enabled
      </span>
    );
  }
  return (
    <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-200 text-gray-600">
      Disabled
    </span>
  );
}

function OperBadge({ value }: { value: boolean | null }) {
  if (value === null) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-500">
        Unknown
      </span>
    );
  }
  if (value) {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
        <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
        Up
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
      <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
      Down
    </span>
  );
}

function ModeBadge({ mode }: { mode: PortMode }) {
  const cls =
    mode === 'access'
      ? 'bg-blue-50 text-blue-700 ring-1 ring-blue-200'
      : mode === 'trunk'
      ? 'bg-purple-50 text-purple-700 ring-1 ring-purple-200'
      : 'bg-gray-100 text-gray-500 ring-1 ring-gray-200';
  const label = mode === 'unknown' ? 'Unknown' : mode.charAt(0).toUpperCase() + mode.slice(1);
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${cls}`}>
      {label}
    </span>
  );
}

// ── Shared select class ───────────────────────────────────────────────────────

const SELECT_CLS =
  'px-2 py-1.5 text-sm border border-gray-300 rounded-md bg-white text-gray-700 focus:outline-none focus:ring-1 focus:ring-blue-400';

// ── Filter bar ────────────────────────────────────────────────────────────────

type AdminFilter = '' | 'enabled' | 'disabled';
type OperFilter = '' | 'up' | 'down';
type SortDir = 'asc' | 'desc';

interface FilterBarProps {
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

function FilterBar(props: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 mb-3">
      <input
        type="search"
        placeholder="Search interface or description..."
        value={props.search}
        onChange={(e) => props.setSearch(e.target.value)}
        className="px-3 py-1.5 text-sm border border-gray-300 rounded-md w-64 focus:outline-none focus:ring-1 focus:ring-blue-400"
      />

      <select
        value={props.filterMode}
        onChange={(e) => props.setFilterMode(e.target.value as PortMode | '')}
        className={SELECT_CLS}
      >
        <option value="">All modes</option>
        <option value="access">Access</option>
        <option value="trunk">Trunk</option>
        <option value="unknown">Unknown</option>
      </select>

      <select
        value={props.filterAdmin}
        onChange={(e) => props.setFilterAdmin(e.target.value as AdminFilter)}
        className={SELECT_CLS}
      >
        <option value="">Any admin state</option>
        <option value="enabled">Admin enabled</option>
        <option value="disabled">Admin disabled</option>
      </select>

      <select
        value={props.filterOper}
        onChange={(e) => props.setFilterOper(e.target.value as OperFilter)}
        className={SELECT_CLS}
      >
        <option value="">Any link state</option>
        <option value="up">Link up</option>
        <option value="down">Link down</option>
      </select>

      {props.hasActiveFilters && (
        <button onClick={props.onClear} className="text-xs text-gray-500 hover:text-gray-700 underline px-1">
          Clear
        </button>
      )}

      <div className="ml-auto flex items-center gap-1.5">
        <span className="text-xs text-gray-400 whitespace-nowrap">Per page:</span>
        <select
          value={props.pageSize}
          onChange={(e) => props.setPageSize(Number(e.target.value))}
          className={SELECT_CLS}
        >
          {PAGE_SIZE_OPTIONS.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ── Pagination (mirrors the jobs page control) ────────────────────────────────

function Pagination({
  page,
  totalPages,
  totalItems,
  pageSize,
  onPrev,
  onNext,
  onGoTo,
}: {
  page: number;
  totalPages: number;
  totalItems: number;
  pageSize: number;
  onPrev: () => void;
  onNext: () => void;
  onGoTo: (p: number) => void;
}) {
  const [inputVal, setInputVal] = useState(String(page));

  const firstItem = totalItems === 0 ? 0 : (page - 1) * pageSize + 1;
  const lastItem = Math.min(page * pageSize, totalItems);

  function handleKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter') return;
    const n = parseInt(inputVal, 10);
    if (!isNaN(n) && n >= 1 && n <= totalPages) onGoTo(n);
    else setInputVal(String(page));
  }

  function handleBlur() {
    const n = parseInt(inputVal, 10);
    if (!isNaN(n) && n >= 1 && n <= totalPages) onGoTo(n);
    else setInputVal(String(page));
  }

  if (totalPages <= 1) return null;

  return (
    <div className="flex items-center justify-between mt-4 text-sm">
      <span className="text-xs text-gray-400">
        {firstItem}–{lastItem} of {totalItems}
      </span>

      <div className="flex items-center gap-2">
        <button
          onClick={onPrev}
          disabled={page <= 1}
          className="px-2.5 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          ← Prev
        </button>

        <div className="flex items-center gap-1 text-xs text-gray-500">
          <span>Page</span>
          <input
            type="text"
            value={inputVal}
            onChange={(e) => setInputVal(e.target.value)}
            onKeyDown={handleKey}
            onBlur={handleBlur}
            className="w-10 px-1.5 py-0.5 text-center border border-gray-300 rounded focus:outline-none focus:ring-1 focus:ring-blue-400"
            aria-label="Page number"
          />
          <span>of {totalPages}</span>
        </div>

        <button
          onClick={onNext}
          disabled={page >= totalPages}
          className="px-2.5 py-1 text-xs border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Next →
        </button>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function PortsPage() {
  const { user } = useAuth();
  const { trackJob } = useJobNotifications();
  const canEdit = !!user && user.role !== 'observer';

  const [selectedDevice, setSelectedDevice] = useState<string>('');
  const [siteFilter, setSiteFilter] = useState<string>('');

  const [search, setSearch] = useState('');
  const [filterMode, setFilterMode] = useState<PortMode | ''>('');
  const [filterAdmin, setFilterAdmin] = useState<AdminFilter>('');
  const [filterOper, setFilterOper] = useState<OperFilter>('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  // ── Inline description-edit state ──
  const [editingPort, setEditingPort] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState<string>('');
  const [savingPort, setSavingPort] = useState<string | null>(null);
  const [editErrorPort, setEditErrorPort] = useState<{ port: string; message: string } | null>(null);

  // ── Admin-state toggle state ──
  // Set of interface names currently mid-toggle so the buttons can be
  // disabled per-row without locking the whole page.
  const [togglingPort, setTogglingPort] = useState<string | null>(null);
  const [adminErrorPort, setAdminErrorPort] = useState<{ port: string; message: string } | null>(null);

  // ── Trunk VLAN inline-edit state ──
  const [editingTrunkPort, setEditingTrunkPort] = useState<string | null>(null);
  const [trunkEditValue, setTrunkEditValue] = useState<string>('');
  const [savingTrunkPort, setSavingTrunkPort] = useState<string | null>(null);
  const [trunkErrorPort, setTrunkErrorPort] = useState<{ port: string; message: string } | null>(null);

  // ── Mode + VLAN composite inline-edit state ──
  const [editingModePort, setEditingModePort] = useState<string | null>(null);
  const [modeEditMode, setModeEditMode] = useState<'access' | 'trunk'>('access');
  const [modeEditAccessVlan, setModeEditAccessVlan] = useState<string>('');
  const [modeEditTrunkVlans, setModeEditTrunkVlans] = useState<string>('');
  const [savingModePort, setSavingModePort] = useState<string | null>(null);
  const [modeErrorPort, setModeErrorPort] = useState<{ port: string; message: string } | null>(null);

  // ── Access VLAN inline-edit state ──
  const [editingAccessVlanPort, setEditingAccessVlanPort] = useState<string | null>(null);
  const [accessVlanEditValue, setAccessVlanEditValue] = useState<string>('');
  const [savingAccessVlanPort, setSavingAccessVlanPort] = useState<string | null>(null);
  const [accessVlanErrorPort, setAccessVlanErrorPort] = useState<{ port: string; message: string } | null>(null);

  // ── Bulk selection + operation state ──
  const [selectedPorts, setSelectedPorts] = useState<Set<string>>(new Set());
  const [bulkExecuting, setBulkExecuting] = useState(false);
  const [bulkErrors, setBulkErrors] = useState<string[]>([]);
  const [bulkVlanInput, setBulkVlanInput] = useState('');
  const [bulkVlanExpanded, setBulkVlanExpanded] = useState(false);
  const [bulkConfirmingDisable, setBulkConfirmingDisable] = useState(false);
  const [bulkConfirmingClearDesc, setBulkConfirmingClearDesc] = useState(false);

  // ── Table sort state ──
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  // ── Per-row inline confirm state ──
  const [confirmingDisablePort, setConfirmingDisablePort] = useState<string | null>(null);

  // ── Device + site lookup ──
  const {
    data: devices,
    isLoading: devicesLoading,
    error: devicesError,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  const filteredDevices = (devices ?? []).filter(
    (d) => !siteFilter || String(d.site_id ?? '') === siteFilter,
  );

  const effectiveSelectedDevice = useMemo(() => {
    if (filteredDevices.length === 0) return '';
    if (selectedDevice && filteredDevices.some(d => d.name === selectedDevice)) {
      return selectedDevice;
    }
    return filteredDevices[0].name;
  }, [selectedDevice, filteredDevices]);

  // ── Port fetch ──
  const {
    data: portsResponse,
    isLoading: portsLoading,
    isFetching: portsFetching,
    error: portsError,
    refetch,
  } = useQuery<PortListResponse>({
    queryKey: ['ports', effectiveSelectedDevice],
    queryFn: () => getPorts(effectiveSelectedDevice),
    enabled: !!effectiveSelectedDevice,
  });


  // ── Apply filters + search client-side ──
  const filteredPorts = useMemo(() => {
    const all = portsResponse?.ports ?? [];
    const q = search.trim().toLowerCase();

    const filtered = all.filter((p) => {
      if (q) {
        const hay = `${p.name} ${p.description ?? ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      if (filterMode && p.mode !== filterMode) return false;
      if (filterAdmin === 'enabled' && p.admin_up !== true) return false;
      if (filterAdmin === 'disabled' && p.admin_up !== false) return false;
      if (filterOper === 'up' && p.operational_up !== true) return false;
      if (filterOper === 'down' && p.operational_up !== false) return false;
      return true;
    });

    return [...filtered].sort((a, b) => {
      const cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [portsResponse, search, filterMode, filterAdmin, filterOper, sortDir]);

  const totalItems = filteredPorts.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  const pageItems: Port[] = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filteredPorts.slice(start, start + pageSize);
  }, [filteredPorts, page, pageSize]);

  const hasActiveFilters = !!search || !!filterMode || !!filterAdmin || !!filterOper;

  const allPageSelected = pageItems.length > 0 && pageItems.every(p => selectedPorts.has(p.name));
  const somePageSelected = pageItems.some(p => selectedPorts.has(p.name));

  const hasNonAccessSelected = useMemo(() => {
    if (selectedPorts.size === 0) return false;
    const portMap = new Map((portsResponse?.ports ?? []).map(p => [p.name, p]));
    return Array.from(selectedPorts).some(name => {
      const p = portMap.get(name);
      return !p || p.mode !== 'access';
    });
  }, [selectedPorts, portsResponse]);

  function clearFilters() {
    setSearch('');
    setFilterMode('');
    setFilterAdmin('');
    setFilterOper('');
    setPage(1);
  }

  function handleSetSelectedDevice(name: string) {
    setSelectedDevice(name);
    setPage(1);
    setSelectedPorts(new Set());
    setBulkErrors([]);
    setBulkVlanExpanded(false);
    setBulkVlanInput('');
    setBulkConfirmingDisable(false);
    setBulkConfirmingClearDesc(false);
    setConfirmingDisablePort(null);
    setEditingModePort(null);
    setModeErrorPort(null);
  }

  function handleSetSearch(v: string) { setSearch(v); setPage(1); }
  function handleSetFilterMode(v: PortMode | '') { setFilterMode(v); setPage(1); }
  function handleSetFilterAdmin(v: AdminFilter) { setFilterAdmin(v); setPage(1); }
  function handleSetFilterOper(v: OperFilter) { setFilterOper(v); setPage(1); }
  function handleSetPageSize(n: number) { setPageSize(n); setPage(1); }

  // ── Inline-edit handlers ──

  function handleEditStart(port: Port) {
    setEditingPort(port.name);
    setEditingValue(port.description ?? '');
    setEditErrorPort(null);
  }

  function handleEditCancel() {
    setEditingPort(null);
    setEditingValue('');
    setEditErrorPort(null);
  }

  async function handleToggleAdminState(port: Port) {
    if (!effectiveSelectedDevice || port.admin_up === null) return;
    const nextEnabled = !port.admin_up;
    setTogglingPort(port.name);
    setAdminErrorPort(null);
    try {
      const result = await configurePort({
        device: effectiveSelectedDevice,
        port_name: port.name,
        enabled: nextEnabled,
      });
      const job = result.jobs[0];
      if (job) {
        trackJob(
          job.job_id,
          `${nextEnabled ? 'Enable' : 'Disable'} ${port.name}`,
          job.device,
        );
      }
      setTimeout(() => { refetch(); }, 500);
    } catch (err) {
      setAdminErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
    } finally {
      setTogglingPort(null);
    }
  }

  async function handleEditSave(port: Port) {
    if (!effectiveSelectedDevice) return;
    const value = editingValue;
    // No-op short-circuit: if the user didn't change anything, just close.
    if ((value ?? '').trim() === (port.description ?? '').trim()) {
      handleEditCancel();
      return;
    }
    setSavingPort(port.name);
    setEditErrorPort(null);
    try {
      const result = await updatePortDescription({
        device: effectiveSelectedDevice,
        interface: port.name,
        description: value,
      });
      const job = result.jobs[0];
      if (job) {
        trackJob(job.job_id, `Update description on ${port.name}`, job.device);
      }
      // Optimistic close + refetch so the new value lands in the table.
      handleEditCancel();
      // Brief delay before the refetch — the backend job runs asynchronously
      // and the new state may not be visible immediately.  This is best-effort
      // UX; the JobNotificationContext drives the authoritative completion signal.
      setTimeout(() => { refetch(); }, 500);
    } catch (err) {
      setEditErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
    } finally {
      setSavingPort(null);
    }
  }

  // ── Trunk VLAN inline-edit handlers ──

  function handleTrunkEditStart(port: Port) {
    setEditingTrunkPort(port.name);
    setTrunkEditValue(formatVlanList(port.allowed_vlans) === DASH ? '' : formatVlanList(port.allowed_vlans));
    setTrunkErrorPort(null);
  }

  function handleTrunkEditCancel() {
    setEditingTrunkPort(null);
    setTrunkEditValue('');
    setTrunkErrorPort(null);
  }

  async function handleTrunkEditSave(port: Port) {
    if (!effectiveSelectedDevice) return;
    const { vlans, error } = parseVlanInput(trunkEditValue);
    if (error) {
      setTrunkErrorPort({ port: port.name, message: error });
      return;
    }
    if (vlans.length === 0) {
      setTrunkErrorPort({ port: port.name, message: 'Enter at least one VLAN ID' });
      return;
    }
    setSavingTrunkPort(port.name);
    setTrunkErrorPort(null);
    try {
      const result = await setTrunkAllowedVlans({
        device: effectiveSelectedDevice,
        interface: port.name,
        mode: 'replace',
        vlans,
      });
      const job = result.jobs[0];
      if (job) {
        trackJob(
          job.job_id,
          `Set trunk VLANs on ${port.name}`,
          job.device,
        );
      }
      handleTrunkEditCancel();
      setTimeout(() => { refetch(); }, 500);
    } catch (err) {
      setTrunkErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
    } finally {
      setSavingTrunkPort(null);
    }
  }

  // ── Access VLAN inline-edit handlers ──

  function handleAccessVlanEditStart(port: Port) {
    setEditingAccessVlanPort(port.name);
    setAccessVlanEditValue(port.access_vlan != null ? String(port.access_vlan) : '');
    setAccessVlanErrorPort(null);
  }

  function handleAccessVlanEditCancel() {
    setEditingAccessVlanPort(null);
    setAccessVlanEditValue('');
    setAccessVlanErrorPort(null);
  }

  async function handleAccessVlanEditSave(port: Port) {
    if (!effectiveSelectedDevice) return;
    const raw = accessVlanEditValue.trim();
    const n = parseInt(raw, 10);

    if (!raw || isNaN(n) || String(n) !== raw) {
      setAccessVlanErrorPort({ port: port.name, message: 'Enter a valid VLAN ID (integer)' });
      return;
    }
    if (n < 1 || n > 4094) {
      setAccessVlanErrorPort({ port: port.name, message: 'VLAN ID must be between 1 and 4094' });
      return;
    }
    if (RESERVED_VLANS.has(n)) {
      setAccessVlanErrorPort({ port: port.name, message: `VLAN ${n} is reserved (Cisco legacy)` });
      return;
    }
    if (port.access_vlan === n) {
      handleAccessVlanEditCancel();
      return;
    }

    setSavingAccessVlanPort(port.name);
    setAccessVlanErrorPort(null);
    try {
      const result = await setPortAccessVlan({
        device: effectiveSelectedDevice,
        interface: port.name,
        vlan_id: n,
      });
      const job = result.jobs[0];
      if (job) {
        trackJob(job.job_id, `Set access VLAN ${n} on ${port.name}`, job.device);
      }
      handleAccessVlanEditCancel();
      setTimeout(() => { refetch(); }, 500);
    } catch (err) {
      setAccessVlanErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
    } finally {
      setSavingAccessVlanPort(null);
    }
  }

  // ── Mode + VLAN composite inline-edit handlers ──

  function handleModeEditStart(port: Port) {
    setEditingModePort(port.name);
    setModeEditMode(port.mode === 'trunk' ? 'trunk' : 'access');
    setModeEditAccessVlan(port.access_vlan != null ? String(port.access_vlan) : '');
    setModeEditTrunkVlans(formatVlanList(port.allowed_vlans) === DASH ? '' : formatVlanList(port.allowed_vlans));
    setModeErrorPort(null);
  }

  function handleModeEditCancel() {
    setEditingModePort(null);
    setModeEditMode('access');
    setModeEditAccessVlan('');
    setModeEditTrunkVlans('');
    setModeErrorPort(null);
  }

  async function handleModeEditSave(port: Port) {
    if (!effectiveSelectedDevice) return;

    const rawVlan = modeEditAccessVlan.trim();
    const vlanNum = parseInt(rawVlan, 10);
    const vlanLabel = modeEditMode === 'trunk' ? 'Native VLAN / PVID' : 'Access VLAN';

    if (!rawVlan || isNaN(vlanNum) || String(vlanNum) !== rawVlan) {
      setModeErrorPort({ port: port.name, message: `${vlanLabel} is required and must be an integer` });
      return;
    }
    if (vlanNum < 1 || vlanNum > 4094) {
      setModeErrorPort({ port: port.name, message: `${vlanLabel} must be between 1 and 4094` });
      return;
    }
    if (RESERVED_VLANS.has(vlanNum)) {
      setModeErrorPort({ port: port.name, message: `VLAN ${vlanNum} is reserved (Cisco legacy)` });
      return;
    }

    let allowedVlans: number[] | undefined;
    if (modeEditMode === 'trunk' && modeEditTrunkVlans.trim()) {
      const { vlans, error } = parseVlanInput(modeEditTrunkVlans);
      if (error) {
        setModeErrorPort({ port: port.name, message: error });
        return;
      }
      allowedVlans = vlans;
    }

    setSavingModePort(port.name);
    setModeErrorPort(null);
    try {
      const result = await configurePort({
        device: effectiveSelectedDevice,
        port_name: port.name,
        mode: modeEditMode,
        access_vlan: vlanNum,
        allowed_vlans: allowedVlans,
      });
      const job = result.jobs[0];
      if (job) {
        trackJob(
          job.job_id,
          `Set mode ${modeEditMode} on ${port.name}`,
          job.device,
        );
      }
      handleModeEditCancel();
      setTimeout(() => { refetch(); }, 500);
    } catch (err) {
      setModeErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
    } finally {
      setSavingModePort(null);
    }
  }

  // ── Bulk operation handlers ──

  async function handleBulkAction(action: 'enable' | 'disable' | 'clear-description') {
    if (!effectiveSelectedDevice || bulkExecuting || selectedPorts.size === 0) return;
    setBulkExecuting(true);
    setBulkErrors([]);
    const errors: string[] = [];

    for (const portName of Array.from(selectedPorts)) {
      try {
        const result = await configurePort(
          action === 'enable'
            ? { device: effectiveSelectedDevice, port_name: portName, enabled: true }
            : action === 'disable'
            ? { device: effectiveSelectedDevice, port_name: portName, enabled: false }
            : { device: effectiveSelectedDevice, port_name: portName, description: '' },
        );
        const job = result.jobs[0];
        if (job) {
          const label = action === 'enable' ? 'Enable' : action === 'disable' ? 'Disable' : 'Clear description on';
          trackJob(job.job_id, `Bulk ${label.toLowerCase()} ${portName}`, job.device);
        }
      } catch (err) {
        errors.push(`${portName}: ${extractMessage(err, 'Failed')}`);
      }
    }

    setBulkExecuting(false);
    setBulkErrors(errors);
    if (errors.length === 0) setSelectedPorts(new Set());
    setTimeout(() => { refetch(); }, 500);
  }

  async function handleBulkSetAccessVlan() {
    if (!effectiveSelectedDevice || bulkExecuting || selectedPorts.size === 0) return;

    const raw = bulkVlanInput.trim();
    const n = parseInt(raw, 10);
    if (!raw || isNaN(n) || String(n) !== raw) {
      setBulkErrors(['Invalid VLAN ID — must be an integer']);
      return;
    }
    if (n < 1 || n > 4094) {
      setBulkErrors(['VLAN ID must be between 1 and 4094']);
      return;
    }
    if (RESERVED_VLANS.has(n)) {
      setBulkErrors([`VLAN ${n} is reserved (Cisco legacy)`]);
      return;
    }

    setBulkExecuting(true);
    setBulkErrors([]);
    const errors: string[] = [];

    for (const portName of Array.from(selectedPorts)) {
      try {
        const result = await configurePort({
          device: effectiveSelectedDevice,
          port_name: portName,
          mode: 'access',
          access_vlan: n,
        });
        const job = result.jobs[0];
        if (job) {
          trackJob(job.job_id, `Bulk set access VLAN ${n} on ${portName}`, job.device);
        }
      } catch (err) {
        errors.push(`${portName}: ${extractMessage(err, 'Failed')}`);
      }
    }

    setBulkExecuting(false);
    setBulkErrors(errors);
    if (errors.length === 0) {
      setSelectedPorts(new Set());
      setBulkVlanExpanded(false);
      setBulkVlanInput('');
    }
    setTimeout(() => { refetch(); }, 500);
  }

  // ── Render ──
  return (
    <div>
      <PageHeader
        title="Port Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={portsLoading || portsFetching || !effectiveSelectedDevice}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {portsFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">
        View and configure port state on managed devices.
      </p>

      {/* Device selection */}
      <div className="mb-6 space-y-3">
        <div className="flex items-center gap-2">
          <label htmlFor="site-filter" className="text-sm text-gray-700">
            Site:
          </label>
          <select
            id="site-filter"
            value={siteFilter}
            onChange={(e) => setSiteFilter(e.target.value)}
            className={SELECT_CLS}
          >
            <option value="">All Sites</option>
            {siteList.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>

          <label htmlFor="device-select" className="text-sm text-gray-700 ml-3">
            Device:
          </label>
          <select
            id="device-select"
            value={effectiveSelectedDevice}
            onChange={(e) => handleSetSelectedDevice(e.target.value)}
            disabled={devicesLoading || filteredDevices.length === 0}
            className={SELECT_CLS}
          >
            {filteredDevices.length === 0 && <option value="">— No devices —</option>}
            {filteredDevices.map((d) => (
              <option key={d.name} value={d.name}>
                {d.name}
                {d.site_name ? ` (${d.site_name})` : ''}
              </option>
            ))}
          </select>

          {portsResponse?.vendor && (
            <span className="text-xs text-gray-400 ml-2">
              vendor: <span className="font-mono">{portsResponse.vendor}</span>
            </span>
          )}
        </div>

        {devicesLoading ? (
          <LoadingSpinner size="sm" />
        ) : devicesError ? (
          <ErrorMessage error={extractMessage(devicesError, 'Could not load devices')} />
        ) : filteredDevices.length === 0 ? (
          <p className="text-sm text-gray-400">
            {siteFilter ? 'No devices in this site.' : 'No devices available.'}
          </p>
        ) : null}
      </div>

      {/* Table area */}
      {!effectiveSelectedDevice ? (
        <div className="py-12 text-center">
          <p className="text-sm text-gray-400">Select a device to view its ports.</p>
        </div>
      ) : portsLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : portsError ? (
        isUnsupportedVendorError(portsError) ? (
          // Vendor unsupported — calmer "not yet available" notice instead of
          // a red error.  Hide the Retry button because retrying will never
          // help; this is a feature-coverage gap, not a transient failure.
          <div className="py-6">
            <div className="rounded-md border border-gray-200 bg-gray-50 px-4 py-4 max-w-xl">
              <div className="flex items-start gap-3">
                <div className="text-gray-400 mt-0.5" aria-hidden>ⓘ</div>
                <div className="text-sm">
                  <p className="font-medium text-gray-800">Port management is not yet supported for this vendor.</p>
                  <p className="mt-1 text-gray-500">
                    {portsResponse?.vendor
                      ? `Selected device reports vendor "${portsResponse.vendor}".`
                      : 'Pick another device to view its port inventory.'}
                  </p>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="py-6">
            <ErrorMessage error={extractMessage(portsError, 'Could not load ports')} />
            <button
              onClick={() => refetch()}
              className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
            >
              Retry
            </button>
          </div>
        )
      ) : (
        <>
          <FilterBar
            search={search}
            setSearch={handleSetSearch}
            filterMode={filterMode}
            setFilterMode={handleSetFilterMode}
            filterAdmin={filterAdmin}
            setFilterAdmin={handleSetFilterAdmin}
            filterOper={filterOper}
            setFilterOper={handleSetFilterOper}
            pageSize={pageSize}
            setPageSize={handleSetPageSize}
            hasActiveFilters={hasActiveFilters}
            onClear={clearFilters}
          />

          {portsFetching && !portsLoading && (
            <div className="h-0.5 bg-blue-100 rounded-full overflow-hidden mb-2">
              <div className="h-full bg-blue-400 rounded-full animate-pulse" />
            </div>
          )}

          <p className="text-xs text-gray-400 mb-2">
            {portsResponse?.count ?? 0} total
            {hasActiveFilters && ` — ${totalItems} match the current filters`}
          </p>

          {/* Bulk action toolbar */}
          {canEdit && selectedPorts.size > 0 && (
            <div className="mb-3">
              <div className="flex flex-wrap items-center gap-2 px-3 py-2 bg-blue-50 rounded-md border border-blue-200">
                <span className="text-sm font-medium text-blue-800 whitespace-nowrap">
                  {selectedPorts.size} port{selectedPorts.size !== 1 ? 's' : ''} selected
                </span>
                {bulkExecuting ? (
                  <span className="text-xs text-blue-600 ml-1 animate-pulse">Executing…</span>
                ) : (
                  <div className="flex flex-wrap items-start gap-1.5 ml-1">
                    <button
                      onClick={() => handleBulkAction('enable')}
                      className="px-2.5 py-1 text-xs text-green-700 border border-green-300 rounded hover:bg-green-50"
                    >
                      Enable
                    </button>
                    {bulkConfirmingDisable ? (
                      <div className="flex items-center gap-1">
                        <span className="text-xs text-red-600 whitespace-nowrap">Disable {selectedPorts.size} port{selectedPorts.size !== 1 ? 's' : ''}?</span>
                        <button onClick={() => { setBulkConfirmingDisable(false); handleBulkAction('disable'); }} className="px-1.5 py-0.5 text-xs bg-red-600 text-white rounded hover:bg-red-700">Yes</button>
                        <button onClick={() => setBulkConfirmingDisable(false)} className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50">No</button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setBulkConfirmingDisable(true)}
                        className="px-2.5 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50"
                      >
                        Disable
                      </button>
                    )}

                    <div className="flex flex-col gap-0.5">
                      {bulkVlanExpanded ? (
                        <div className="flex items-center gap-1">
                          <input
                            type="number"
                            min={1}
                            max={4094}
                            value={bulkVlanInput}
                            onChange={(e) => { setBulkVlanInput(e.target.value); setBulkErrors([]); }}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter') { e.preventDefault(); handleBulkSetAccessVlan(); }
                              if (e.key === 'Escape') { e.preventDefault(); setBulkVlanExpanded(false); setBulkVlanInput(''); setBulkErrors([]); }
                            }}
                            autoFocus
                            placeholder="1–4094"
                            className="w-20 border border-gray-300 rounded px-2 py-0.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
                          />
                          <button
                            onClick={handleBulkSetAccessVlan}
                            disabled={!bulkVlanInput.trim()}
                            className="px-2 py-0.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Apply
                          </button>
                          <button
                            onClick={() => { setBulkVlanExpanded(false); setBulkVlanInput(''); setBulkErrors([]); }}
                            className="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
                          >
                            ×
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={() => { setBulkVlanExpanded(true); setBulkErrors([]); }}
                          disabled={hasNonAccessSelected}
                          className="px-2.5 py-1 text-xs text-blue-700 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
                          title={hasNonAccessSelected ? 'All selected ports must be in access mode' : 'Set access VLAN on selected ports'}
                        >
                          Set Access VLAN
                        </button>
                      )}
                      {hasNonAccessSelected && !bulkVlanExpanded && (
                        <span className="text-xs text-gray-400">Non-access ports in selection</span>
                      )}
                    </div>

                    {bulkConfirmingClearDesc ? (
                      <div className="flex items-center gap-1">
                        <span className="text-xs text-gray-600 whitespace-nowrap">Clear desc on {selectedPorts.size} port{selectedPorts.size !== 1 ? 's' : ''}?</span>
                        <button onClick={() => { setBulkConfirmingClearDesc(false); handleBulkAction('clear-description'); }} className="px-1.5 py-0.5 text-xs bg-gray-700 text-white rounded hover:bg-gray-800">Yes</button>
                        <button onClick={() => setBulkConfirmingClearDesc(false)} className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50">No</button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setBulkConfirmingClearDesc(true)}
                        className="px-2.5 py-1 text-xs text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
                      >
                        Clear Description
                      </button>
                    )}
                    <button
                      onClick={() => { setSelectedPorts(new Set()); setBulkVlanExpanded(false); setBulkVlanInput(''); setBulkErrors([]); setBulkConfirmingDisable(false); setBulkConfirmingClearDesc(false); }}
                      className="px-2.5 py-1 text-xs text-gray-500 border border-gray-200 rounded hover:bg-gray-100"
                    >
                      Clear Selection
                    </button>
                  </div>
                )}
              </div>
              {bulkErrors.length > 0 && (
                <div className="mt-1.5 px-3 py-2 bg-red-50 border border-red-200 rounded-md">
                  <p className="text-xs font-medium text-red-700 mb-1">
                    {bulkErrors.length} error{bulkErrors.length !== 1 ? 's' : ''} during bulk operation:
                  </p>
                  <ul className="text-xs text-red-600 space-y-0.5 max-h-24 overflow-y-auto">
                    {bulkErrors.map((e, i) => <li key={i}>{e}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}

          {totalItems === 0 ? (
            <div className="py-12 text-center">
              <p className="text-gray-400 text-sm">
                {hasActiveFilters
                  ? 'No ports match the current filters.'
                  : 'No ports reported by this device.'}
              </p>
              {hasActiveFilters && (
                <button onClick={clearFilters} className="mt-2 text-xs text-blue-600 hover:underline">
                  Clear filters
                </button>
              )}
            </div>
          ) : (
            <>
              <div className="overflow-auto max-h-[calc(100vh-380px)] border border-gray-200 rounded-md">
                <table className="w-full border-collapse text-sm">
                  <thead className="sticky top-0 z-10 bg-gray-50">
                    <tr className="border-b border-gray-200">
                      <th className="px-3 py-2 w-8">
                        {canEdit && (
                          <input
                            type="checkbox"
                            checked={allPageSelected}
                            ref={(el) => { if (el) el.indeterminate = somePageSelected && !allPageSelected; }}
                            onChange={(e) => {
                              if (e.target.checked) {
                                setSelectedPorts(prev => new Set([...prev, ...pageItems.map(p => p.name)]));
                              } else {
                                setSelectedPorts(prev => {
                                  const next = new Set(prev);
                                  pageItems.forEach(p => next.delete(p.name));
                                  return next;
                                });
                              }
                            }}
                            disabled={bulkExecuting}
                            className="w-4 h-4 accent-blue-600 cursor-pointer disabled:cursor-not-allowed"
                            aria-label="Select all visible ports"
                          />
                        )}
                      </th>
                      <th
                        className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap cursor-pointer select-none hover:bg-gray-100"
                        onClick={() => setSortDir(d => d === 'asc' ? 'desc' : 'asc')}
                        aria-sort={sortDir === 'asc' ? 'ascending' : 'descending'}
                      >
                        Interface <span className="text-gray-400 text-xs">{sortDir === 'asc' ? '↑' : '↓'}</span>
                      </th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700">Description</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Admin</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Link</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Mode</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Access VLAN</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700">Allowed VLANs</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">PoE</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Speed</th>
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Duplex</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pageItems.map((port) => (
                      <tr
                        key={port.name}
                        className={`border-b border-gray-100 transition-colors ${
                          selectedPorts.has(port.name)
                            ? 'bg-blue-50 hover:bg-blue-100'
                            : port.admin_up === false
                            ? 'bg-gray-50 hover:bg-gray-100'
                            : 'hover:bg-gray-50'
                        }`}
                      >
                        <td className="px-3 py-2 w-8">
                          {canEdit && (
                            <input
                              type="checkbox"
                              checked={selectedPorts.has(port.name)}
                              onChange={(e) => {
                                setSelectedPorts(prev => {
                                  const next = new Set(prev);
                                  if (e.target.checked) next.add(port.name);
                                  else next.delete(port.name);
                                  return next;
                                });
                              }}
                              disabled={bulkExecuting}
                              className="w-4 h-4 accent-blue-600 cursor-pointer disabled:cursor-not-allowed"
                              aria-label={`Select ${port.name}`}
                            />
                          )}
                        </td>
                        <td className={`px-3 py-2 font-mono text-xs whitespace-nowrap ${port.admin_up === false ? 'text-gray-400' : 'text-gray-900'}`}>
                          {port.name}
                        </td>
                        <td className="px-3 py-2 text-gray-900 max-w-xs">
                          {editingPort === port.name ? (
                            <div className="flex flex-col gap-1">
                              <div className="flex items-center gap-1">
                                <input
                                  type="text"
                                  value={editingValue}
                                  onChange={(e) => setEditingValue(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') { e.preventDefault(); handleEditSave(port); }
                                    if (e.key === 'Escape') { e.preventDefault(); handleEditCancel(); }
                                  }}
                                  disabled={savingPort === port.name}
                                  maxLength={200}
                                  autoFocus
                                  placeholder="(empty clears description)"
                                  aria-label={`Description for ${port.name}`}
                                  className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                                />
                                <button
                                  onClick={() => handleEditSave(port)}
                                  disabled={savingPort === port.name}
                                  className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                                  aria-label="Save description"
                                >
                                  {savingPort === port.name ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
                                </button>
                                <button
                                  onClick={handleEditCancel}
                                  disabled={savingPort === port.name}
                                  className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                  aria-label="Cancel"
                                >
                                  Cancel
                                </button>
                              </div>
                              {editErrorPort?.port === port.name && (
                                <p className="text-xs text-red-600">{editErrorPort.message}</p>
                              )}
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <span className="text-gray-900 truncate" title={port.description ?? ''}>
                                {port.description ?? DASH}
                              </span>
                              {canEdit && (
                                <button
                                  onClick={() => handleEditStart(port)}
                                  disabled={
                                    bulkExecuting ||
                                    editingPort !== null || savingPort !== null ||
                                    editingTrunkPort !== null || editingAccessVlanPort !== null ||
                                    editingModePort !== null
                                  }
                                  className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed"
                                  aria-label={`Edit description for ${port.name}`}
                                  title="Edit description"
                                >
                                  ✎
                                </button>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex flex-col gap-1">
                            <div className="flex items-center gap-2">
                              <AdminBadge value={port.admin_up} />
                              {canEdit && (
                                togglingPort === port.name ? (
                                  <RowSpinner />
                                ) : confirmingDisablePort === port.name ? (
                                  <div className="flex items-center gap-1">
                                    <span className="text-xs text-red-600 whitespace-nowrap">Disable?</span>
                                    <button
                                      onClick={() => { setConfirmingDisablePort(null); handleToggleAdminState(port); }}
                                      className="px-1.5 py-0.5 text-xs bg-red-600 text-white rounded hover:bg-red-700"
                                    >
                                      Yes
                                    </button>
                                    <button
                                      onClick={() => setConfirmingDisablePort(null)}
                                      className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50"
                                    >
                                      No
                                    </button>
                                  </div>
                                ) : (
                                  <button
                                    onClick={() =>
                                      port.admin_up === true
                                        ? setConfirmingDisablePort(port.name)
                                        : handleToggleAdminState(port)
                                    }
                                    disabled={bulkExecuting || togglingPort !== null || port.admin_up === null}
                                    className={`px-2 py-0.5 text-xs rounded border whitespace-nowrap disabled:opacity-40 disabled:cursor-not-allowed ${
                                      port.admin_up === null
                                        ? 'text-gray-400 border-gray-200'
                                        : port.admin_up
                                        ? 'text-red-600 border-red-300 hover:bg-red-50'
                                        : 'text-green-700 border-green-300 hover:bg-green-50'
                                    }`}
                                    aria-label={
                                      port.admin_up === null
                                        ? `Admin state unknown for ${port.name}`
                                        : `${port.admin_up ? 'Disable' : 'Enable'} ${port.name}`
                                    }
                                    title={
                                      port.admin_up === null
                                        ? 'Admin state unknown'
                                        : port.admin_up
                                        ? 'Disable interface'
                                        : 'Enable interface'
                                    }
                                  >
                                    {port.admin_up === null ? '—' : port.admin_up ? 'Disable' : 'Enable'}
                                  </button>
                                )
                              )}
                            </div>
                            {adminErrorPort?.port === port.name && (
                              <p className="text-xs text-red-600">{adminErrorPort.message}</p>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2"><OperBadge value={port.operational_up} /></td>
                        <td className="px-3 py-2">
                          {editingModePort === port.name ? (
                            <div className="flex flex-col gap-1.5 min-w-[280px]">
                              <div className="flex items-center gap-1.5 flex-wrap">
                                <select
                                  value={modeEditMode}
                                  onChange={(e) => {
                                    setModeEditMode(e.target.value as 'access' | 'trunk');
                                    setModeErrorPort(null);
                                  }}
                                  disabled={savingModePort === port.name}
                                  className="px-1.5 py-1 text-xs border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
                                  aria-label="Port mode"
                                >
                                  <option value="access">Access</option>
                                  <option value="trunk">Trunk</option>
                                </select>
                                <span className="text-xs text-gray-500 whitespace-nowrap">
                                  {modeEditMode === 'trunk' ? 'Native VLAN / PVID:' : 'Access VLAN:'}
                                </span>
                                <input
                                  type="number"
                                  min={1}
                                  max={4094}
                                  value={modeEditAccessVlan}
                                  onChange={(e) => {
                                    setModeEditAccessVlan(e.target.value);
                                    setModeErrorPort(null);
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') { e.preventDefault(); handleModeEditSave(port); }
                                    if (e.key === 'Escape') { e.preventDefault(); handleModeEditCancel(); }
                                  }}
                                  disabled={savingModePort === port.name}
                                  autoFocus
                                  placeholder="1-4094"
                                  aria-label={modeEditMode === 'trunk' ? `Native VLAN for ${port.name}` : `Access VLAN for ${port.name}`}
                                  className="w-20 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                                />
                              </div>
                              {modeEditMode === 'trunk' && (
                                <div className="flex items-center gap-1">
                                  <span className="text-xs text-gray-500 whitespace-nowrap">Allowed VLANs:</span>
                                  <input
                                    type="text"
                                    value={modeEditTrunkVlans}
                                    onChange={(e) => {
                                      setModeEditTrunkVlans(e.target.value);
                                      const { error } = parseVlanInput(e.target.value);
                                      if (error && e.target.value.trim()) {
                                        setModeErrorPort({ port: port.name, message: error });
                                      } else {
                                        setModeErrorPort(null);
                                      }
                                    }}
                                    onKeyDown={(e) => {
                                      if (e.key === 'Enter') { e.preventDefault(); handleModeEditSave(port); }
                                      if (e.key === 'Escape') { e.preventDefault(); handleModeEditCancel(); }
                                    }}
                                    disabled={savingModePort === port.name}
                                    placeholder="e.g. 10,20,30-35 (optional)"
                                    aria-label={`Allowed VLANs for ${port.name}`}
                                    className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                                  />
                                </div>
                              )}
                              <div className="flex items-center gap-1">
                                <button
                                  onClick={() => handleModeEditSave(port)}
                                  disabled={savingModePort === port.name || (modeErrorPort?.port === port.name && !!modeErrorPort?.message)}
                                  className="px-2 py-0.5 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  {savingModePort === port.name ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
                                </button>
                                <button
                                  onClick={handleModeEditCancel}
                                  disabled={savingModePort === port.name}
                                  className="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  Cancel
                                </button>
                              </div>
                              {modeErrorPort?.port === port.name && (
                                <p className="text-xs text-red-600">{modeErrorPort.message}</p>
                              )}
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <ModeBadge mode={port.mode} />
                              {canEdit && port.mode !== 'unknown' && (
                                <button
                                  onClick={() => handleModeEditStart(port)}
                                  disabled={
                                    bulkExecuting ||
                                    editingModePort !== null || savingModePort !== null ||
                                    editingPort !== null || editingTrunkPort !== null ||
                                    editingAccessVlanPort !== null
                                  }
                                  className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
                                  aria-label={`Edit mode for ${port.name}`}
                                  title="Configure port mode and VLANs"
                                >
                                  ✎
                                </button>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-900">
                          {editingAccessVlanPort === port.name ? (
                            <div className="flex flex-col gap-1 min-w-[140px]">
                              <div className="flex items-center gap-1">
                                <input
                                  type="number"
                                  min={1}
                                  max={4094}
                                  value={accessVlanEditValue}
                                  onChange={(e) => {
                                    setAccessVlanEditValue(e.target.value);
                                    setAccessVlanErrorPort(null);
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') { e.preventDefault(); handleAccessVlanEditSave(port); }
                                    if (e.key === 'Escape') { e.preventDefault(); handleAccessVlanEditCancel(); }
                                  }}
                                  disabled={savingAccessVlanPort === port.name}
                                  autoFocus
                                  placeholder="1–4094"
                                  aria-label={`Access VLAN for ${port.name}`}
                                  className="w-20 border border-gray-300 rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                                />
                                <button
                                  onClick={() => handleAccessVlanEditSave(port)}
                                  disabled={savingAccessVlanPort === port.name}
                                  className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  {savingAccessVlanPort === port.name ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
                                </button>
                                <button
                                  onClick={handleAccessVlanEditCancel}
                                  disabled={savingAccessVlanPort === port.name}
                                  className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  Cancel
                                </button>
                              </div>
                              {accessVlanErrorPort?.port === port.name && (
                                <p className="text-xs text-red-600">{accessVlanErrorPort.message}</p>
                              )}
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <span className="font-mono text-xs">{port.access_vlan ?? DASH}</span>
                              {canEdit && (port.mode === 'access' || port.mode === 'trunk') && (
                                <button
                                  onClick={() => handleAccessVlanEditStart(port)}
                                  disabled={
                                    bulkExecuting ||
                                    editingAccessVlanPort !== null || savingAccessVlanPort !== null ||
                                    editingPort !== null || editingTrunkPort !== null ||
                                    editingModePort !== null
                                  }
                                  className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
                                  aria-label={`Edit ${port.mode === 'trunk' ? 'native VLAN' : 'access VLAN'} for ${port.name}`}
                                  title={port.mode === 'trunk' ? 'Edit native VLAN (PVID)' : 'Edit access VLAN'}
                                >
                                  ✎
                                </button>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-900 max-w-xs">
                          {editingTrunkPort === port.name ? (
                            <div className="flex flex-col gap-1.5 min-w-[260px]">
                              <div className="flex items-center gap-1">
                                <input
                                  type="text"
                                  value={trunkEditValue}
                                  onChange={(e) => {
                                    setTrunkEditValue(e.target.value);
                                    // Live validation feedback
                                    const { error } = parseVlanInput(e.target.value);
                                    if (error && e.target.value.trim()) {
                                      setTrunkErrorPort({ port: port.name, message: error });
                                    } else {
                                      setTrunkErrorPort(null);
                                    }
                                  }}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') { e.preventDefault(); handleTrunkEditSave(port); }
                                    if (e.key === 'Escape') { e.preventDefault(); handleTrunkEditCancel(); }
                                  }}
                                  disabled={savingTrunkPort === port.name}
                                  autoFocus
                                  placeholder="e.g. 10,20,30-35"
                                  aria-label={`Trunk VLANs for ${port.name}`}
                                  className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                                />
                              </div>
                              <div className="flex items-center gap-1">
                                <button
                                  onClick={() => handleTrunkEditSave(port)}
                                  disabled={savingTrunkPort === port.name || (trunkErrorPort?.port === port.name && !!trunkErrorPort?.message && trunkEditValue.trim() !== '')}
                                  className="px-2 py-0.5 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  {savingTrunkPort === port.name ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
                                </button>
                                <button
                                  onClick={handleTrunkEditCancel}
                                  disabled={savingTrunkPort === port.name}
                                  className="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  Cancel
                                </button>
                              </div>
                              {trunkErrorPort?.port === port.name && (
                                <p className="text-xs text-red-600">{trunkErrorPort.message}</p>
                              )}
                            </div>
                          ) : (
                            <div className="flex items-center gap-1.5">
                              <span className="truncate font-mono text-xs" title={formatVlanList(port.allowed_vlans)}>
                                {formatVlanList(port.allowed_vlans)}
                              </span>
                              {canEdit && port.mode === 'trunk' && (
                                <button
                                  onClick={() => handleTrunkEditStart(port)}
                                  disabled={
                                    bulkExecuting ||
                                    editingTrunkPort !== null || savingTrunkPort !== null ||
                                    editingPort !== null || editingAccessVlanPort !== null ||
                                    editingModePort !== null
                                  }
                                  className="opacity-60 hover:opacity-100 text-gray-500 hover:text-purple-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
                                  aria-label={`Edit trunk VLANs for ${port.name}`}
                                  title="Edit trunk allowed VLANs"
                                >
                                  ✎
                                </button>
                              )}
                            </div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-gray-600">
                          {port.poe_enabled === null
                            ? 'N/A'
                            : port.poe_enabled
                            ? 'On'
                            : 'Off'}
                        </td>
                        <td className="px-3 py-2 text-gray-600">{port.speed ?? 'N/A'}</td>
                        <td className="px-3 py-2 text-gray-600">{port.duplex ?? 'N/A'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <Pagination
                key={page}
                page={page}
                totalPages={totalPages}
                totalItems={totalItems}
                pageSize={pageSize}
                onPrev={() => setPage((p) => Math.max(1, p - 1))}
                onNext={() => setPage((p) => Math.min(totalPages, p + 1))}
                onGoTo={(p) => setPage(p)}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
