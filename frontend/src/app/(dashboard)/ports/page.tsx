'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import {
  clearPortDescription,
  getDevices,
  getPorts,
  getSites,
  setPortAccessMode,
  setPortAccessVlan,
  setPortAdminState,
  setPortTrunkMode,
  setTrunkAllowedVlans,
  updatePortDescription,
} from '@/services/api';
import type { Device } from '@/types/device';
import type { Site } from '@/types/site';
import type { Port, PortListResponse, PortMode, TrunkVlanMode } from '@/types/port';

import {
  DASH,
  RESERVED_VLANS,
  SELECT_CLS,
  extractMessage,
  formatVlanList,
  isUnsupportedVendorError,
  parseVlanInput,
} from './helpers';
import type {
  AccessVlanEditor,
  AdminFilter,
  AdminToggler,
  BulkOps,
  DescriptionEditor,
  EditorsBusy,
  ModeEditor,
  OperFilter,
  PortError,
  SortDir,
  TrunkVlansEditor,
} from './types';
import { BulkActionToolbar } from './components/BulkActionToolbar';
import { FilterBar } from './components/FilterBar';
import { Pagination } from './components/Pagination';
import { PortTable } from './components/PortTable';

export default function PortsPage() {
  const { user } = useAuth();
  const { trackJob } = useJobNotifications();
  const canEdit = !!user && user.role !== 'observer';

  // ── Device / site selection ────────────────────────────────────────────────
  const [selectedDevice, setSelectedDevice] = useState('');
  const [siteFilter, setSiteFilter] = useState('');

  // ── List filters + pagination ──────────────────────────────────────────────
  const [search, setSearch] = useState('');
  const [filterMode, setFilterMode] = useState<PortMode | ''>('');
  const [filterAdmin, setFilterAdmin] = useState<AdminFilter>('');
  const [filterOper, setFilterOper] = useState<OperFilter>('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  // ── Inline editor state (one block per editable cell) ──────────────────────
  const [editingPort, setEditingPort] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState('');
  const [savingPort, setSavingPort] = useState<string | null>(null);
  const [editErrorPort, setEditErrorPort] = useState<PortError>(null);

  const [togglingPort, setTogglingPort] = useState<string | null>(null);
  const [adminErrorPort, setAdminErrorPort] = useState<PortError>(null);
  const [confirmingDisablePort, setConfirmingDisablePort] = useState<string | null>(null);

  const [editingTrunkPort, setEditingTrunkPort] = useState<string | null>(null);
  const [trunkEditValue, setTrunkEditValue] = useState('');
  const [trunkEditMode, setTrunkEditMode] = useState<TrunkVlanMode>('add');
  const [savingTrunkPort, setSavingTrunkPort] = useState<string | null>(null);
  const [trunkErrorPort, setTrunkErrorPort] = useState<PortError>(null);

  const [editingModePort, setEditingModePort] = useState<string | null>(null);
  const [modeEditMode, setModeEditMode] = useState<'access' | 'trunk'>('access');
  const [modeEditAccessVlan, setModeEditAccessVlan] = useState('');
  const [modeEditTrunkVlans, setModeEditTrunkVlans] = useState('');
  const [savingModePort, setSavingModePort] = useState<string | null>(null);
  const [modeErrorPort, setModeErrorPort] = useState<PortError>(null);

  const [editingAccessVlanPort, setEditingAccessVlanPort] = useState<string | null>(null);
  const [accessVlanEditValue, setAccessVlanEditValue] = useState('');
  const [savingAccessVlanPort, setSavingAccessVlanPort] = useState<string | null>(null);
  const [accessVlanErrorPort, setAccessVlanErrorPort] = useState<PortError>(null);

  // ── Bulk selection + operation state ───────────────────────────────────────
  const [selectedPorts, setSelectedPortsRaw] = useState<Set<string>>(new Set());
  const [bulkExecuting, setBulkExecuting] = useState(false);
  const [bulkErrors, setBulkErrors] = useState<string[]>([]);
  const [bulkVlanInput, setBulkVlanInput] = useState('');
  const [bulkVlanExpanded, setBulkVlanExpanded] = useState(false);
  const [bulkConfirmingDisable, setBulkConfirmingDisable] = useState(false);
  const [bulkConfirmingClearDesc, setBulkConfirmingClearDesc] = useState(false);

  // ── Device + site lookup ───────────────────────────────────────────────────
  const {
    data: devices,
    isLoading: devicesLoading,
    error: devicesError,
  } = useQuery<Device[]>({ queryKey: ['devices'], queryFn: getDevices });

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  const filteredDevices = (devices ?? []).filter(
    (d) => !siteFilter || String(d.site_id ?? '') === siteFilter,
  );

  const effectiveSelectedDevice = useMemo(() => {
    if (filteredDevices.length === 0) return '';
    if (selectedDevice && filteredDevices.some((d) => d.name === selectedDevice)) {
      return selectedDevice;
    }
    return filteredDevices[0].name;
  }, [selectedDevice, filteredDevices]);

  // ── Port fetch ─────────────────────────────────────────────────────────────
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

  // ── Client-side filter / sort / paginate ───────────────────────────────────
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

  const allPageSelected = pageItems.length > 0 && pageItems.every((p) => selectedPorts.has(p.name));
  const somePageSelected = pageItems.some((p) => selectedPorts.has(p.name));

  const hasNonAccessSelected = useMemo(() => {
    if (selectedPorts.size === 0) return false;
    const portMap = new Map((portsResponse?.ports ?? []).map((p) => [p.name, p]));
    return Array.from(selectedPorts).some((name) => {
      const p = portMap.get(name);
      return !p || p.mode !== 'access';
    });
  }, [selectedPorts, portsResponse]);

  // ── Filter / pagination handlers ───────────────────────────────────────────
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
    setSelectedPortsRaw(new Set());
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

  // ── Editor: description ────────────────────────────────────────────────────
  const description: DescriptionEditor = {
    editingPort,
    editingValue,
    setEditingValue,
    savingPort,
    errorPort: editErrorPort,
    start: (port) => {
      setEditingPort(port.name);
      setEditingValue(port.description ?? '');
      setEditErrorPort(null);
    },
    cancel: () => {
      setEditingPort(null);
      setEditingValue('');
      setEditErrorPort(null);
    },
    save: async (port) => {
      if (!effectiveSelectedDevice) return;
      const value = editingValue;
      if ((value ?? '').trim() === (port.description ?? '').trim()) {
        description.cancel();
        return;
      }
      setSavingPort(port.name);
      setEditErrorPort(null);
      try {
        const trimmed = value.trim();
        const result = trimmed
          ? await updatePortDescription(effectiveSelectedDevice, { interface: port.name, description: trimmed })
          : await clearPortDescription(effectiveSelectedDevice, { interface: port.name });
        const job = result.jobs[0];
        if (job) trackJob(job.job_id, `Update description on ${port.name}`, job.device);
        description.cancel();
        setTimeout(() => { refetch(); }, 500);
      } catch (err) {
        setEditErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
      } finally {
        setSavingPort(null);
      }
    },
  };

  // ── Editor: admin toggle ───────────────────────────────────────────────────
  const adminToggler: AdminToggler = {
    togglingPort,
    errorPort: adminErrorPort,
    confirmingPort: confirmingDisablePort,
    setConfirmingPort: setConfirmingDisablePort,
    toggle: async (port) => {
      if (!effectiveSelectedDevice || port.admin_up === null) return;
      const nextEnabled = !port.admin_up;
      setTogglingPort(port.name);
      setAdminErrorPort(null);
      try {
        const result = await setPortAdminState(effectiveSelectedDevice, {
          interface: port.name,
          enabled: nextEnabled,
        });
        const job = result.jobs[0];
        if (job) trackJob(job.job_id, `${nextEnabled ? 'Enable' : 'Disable'} ${port.name}`, job.device);
        setTimeout(() => { refetch(); }, 500);
      } catch (err) {
        setAdminErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
      } finally {
        setTogglingPort(null);
      }
    },
  };

  // ── Editor: trunk VLANs ────────────────────────────────────────────────────
  const trunkVlansEditor: TrunkVlansEditor = {
    editingPort: editingTrunkPort,
    value: trunkEditValue,
    setValue: setTrunkEditValue,
    mode: trunkEditMode,
    setMode: setTrunkEditMode,
    setError: setTrunkErrorPort,
    savingPort: savingTrunkPort,
    errorPort: trunkErrorPort,
    start: (port) => {
      setEditingTrunkPort(port.name);
      setTrunkEditValue(formatVlanList(port.allowed_vlans) === DASH ? '' : formatVlanList(port.allowed_vlans));
      setTrunkEditMode('add');
      setTrunkErrorPort(null);
    },
    cancel: () => {
      setEditingTrunkPort(null);
      setTrunkEditValue('');
      setTrunkErrorPort(null);
    },
    save: async (port) => {
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
        const result = await setTrunkAllowedVlans(effectiveSelectedDevice, {
          interface: port.name,
          mode: trunkEditMode,
          vlans,
        });
        const job = result.jobs[0];
        if (job) {
          const opLabel = trunkEditMode === 'replace' ? 'Set' : trunkEditMode === 'add' ? 'Add to' : 'Remove from';
          trackJob(job.job_id, `${opLabel} trunk VLANs on ${port.name}`, job.device);
        }
        trunkVlansEditor.cancel();
        setTimeout(() => { refetch(); }, 500);
      } catch (err) {
        setTrunkErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
      } finally {
        setSavingTrunkPort(null);
      }
    },
  };

  // ── Editor: access VLAN ────────────────────────────────────────────────────
  const accessVlanEditor: AccessVlanEditor = {
    editingPort: editingAccessVlanPort,
    value: accessVlanEditValue,
    setValue: setAccessVlanEditValue,
    savingPort: savingAccessVlanPort,
    errorPort: accessVlanErrorPort,
    start: (port) => {
      setEditingAccessVlanPort(port.name);
      setAccessVlanEditValue(port.access_vlan != null ? String(port.access_vlan) : '');
      setAccessVlanErrorPort(null);
    },
    cancel: () => {
      setEditingAccessVlanPort(null);
      setAccessVlanEditValue('');
      setAccessVlanErrorPort(null);
    },
    save: async (port) => {
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
        accessVlanEditor.cancel();
        return;
      }
      setSavingAccessVlanPort(port.name);
      setAccessVlanErrorPort(null);
      try {
        const result = await setPortAccessVlan(effectiveSelectedDevice, {
          interface: port.name,
          vlan_id: n,
        });
        const job = result.jobs[0];
        if (job) trackJob(job.job_id, `Set access VLAN ${n} on ${port.name}`, job.device);
        accessVlanEditor.cancel();
        setTimeout(() => { refetch(); }, 500);
      } catch (err) {
        setAccessVlanErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
      } finally {
        setSavingAccessVlanPort(null);
      }
    },
  };

  // ── Editor: mode + VLAN composite ──────────────────────────────────────────
  // Routes to setPortAccessMode() (access) / setPortTrunkMode() (trunk) --
  // 2 named endpoints instead of the old generic configurePort()/
  // /ports/configure. Trunk mode now requires BOTH native_vlan (PVID) and
  // allowed_vlans explicitly -- no more optional allowed-VLANs / add-remove
  // operation for a genuine mode change (that stays available via the
  // separate access-vlan/trunk-vlans editors for a port that's already in
  // the target mode).
  const modeEditor: ModeEditor = {
    editingPort: editingModePort,
    mode: modeEditMode,
    setMode: setModeEditMode,
    accessVlan: modeEditAccessVlan,
    setAccessVlan: setModeEditAccessVlan,
    trunkVlans: modeEditTrunkVlans,
    setTrunkVlans: setModeEditTrunkVlans,
    setError: setModeErrorPort,
    savingPort: savingModePort,
    errorPort: modeErrorPort,
    start: (port) => {
      setEditingModePort(port.name);
      setModeEditMode(port.mode === 'trunk' ? 'trunk' : 'access');
      setModeEditAccessVlan(port.access_vlan != null ? String(port.access_vlan) : '');
      setModeEditTrunkVlans(formatVlanList(port.allowed_vlans) === DASH ? '' : formatVlanList(port.allowed_vlans));
      setModeErrorPort(null);
    },
    cancel: () => {
      setEditingModePort(null);
      setModeEditMode('access');
      setModeEditAccessVlan('');
      setModeEditTrunkVlans('');
      setModeErrorPort(null);
    },
    save: async (port) => {
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

      let allowedVlans: number[] = [];
      if (modeEditMode === 'trunk') {
        if (!modeEditTrunkVlans.trim()) {
          setModeErrorPort({ port: port.name, message: 'Allowed VLANs are required for trunk mode' });
          return;
        }
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
        const result = modeEditMode === 'trunk'
          ? await setPortTrunkMode(effectiveSelectedDevice, {
              interface: port.name,
              native_vlan: vlanNum,
              allowed_vlans: allowedVlans,
            })
          : await setPortAccessMode(effectiveSelectedDevice, {
              interface: port.name,
              access_vlan: vlanNum,
            });
        const job = result.jobs[0];
        if (job) trackJob(job.job_id, `Set mode ${modeEditMode} on ${port.name}`, job.device);
        modeEditor.cancel();
        setTimeout(() => { refetch(); }, 500);
      } catch (err) {
        setModeErrorPort({ port: port.name, message: extractMessage(err, 'Update failed') });
      } finally {
        setSavingModePort(null);
      }
    },
  };

  // ── Bulk operations ────────────────────────────────────────────────────────
  function setSelectedPorts(next: Set<string>) { setSelectedPortsRaw(next); }

  const bulk: BulkOps = {
    selectedPorts,
    setSelectedPorts,
    executing: bulkExecuting,
    errors: bulkErrors,
    setErrors: setBulkErrors,
    vlanInput: bulkVlanInput,
    setVlanInput: setBulkVlanInput,
    vlanExpanded: bulkVlanExpanded,
    setVlanExpanded: setBulkVlanExpanded,
    confirmingDisable: bulkConfirmingDisable,
    setConfirmingDisable: setBulkConfirmingDisable,
    confirmingClearDesc: bulkConfirmingClearDesc,
    setConfirmingClearDesc: setBulkConfirmingClearDesc,
    hasNonAccessSelected,
    runAction: async (action) => {
      if (!effectiveSelectedDevice || bulkExecuting || selectedPorts.size === 0) return;
      setBulkExecuting(true);
      setBulkErrors([]);
      const errors: string[] = [];
      for (const portName of Array.from(selectedPorts)) {
        try {
          const result = action === 'clear-description'
            ? await clearPortDescription(effectiveSelectedDevice, { interface: portName })
            : await setPortAdminState(effectiveSelectedDevice, { interface: portName, enabled: action === 'enable' });
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
      if (errors.length === 0) setSelectedPortsRaw(new Set());
      setTimeout(() => { refetch(); }, 500);
    },
    setAccessVlan: async () => {
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
          const result = await setPortAccessMode(effectiveSelectedDevice, {
            interface: portName,
            access_vlan: n,
          });
          const job = result.jobs[0];
          if (job) trackJob(job.job_id, `Bulk set access VLAN ${n} on ${portName}`, job.device);
        } catch (err) {
          errors.push(`${portName}: ${extractMessage(err, 'Failed')}`);
        }
      }
      setBulkExecuting(false);
      setBulkErrors(errors);
      if (errors.length === 0) {
        setSelectedPortsRaw(new Set());
        setBulkVlanExpanded(false);
        setBulkVlanInput('');
      }
      setTimeout(() => { refetch(); }, 500);
    },
  };

  // ── Busy bundle used to disable row-level edit buttons while any other
  //    editor / bulk op is in flight. Computed once so each row gets a
  //    stable reference.
  const anyEditing =
    editingPort !== null ||
    editingTrunkPort !== null ||
    editingAccessVlanPort !== null ||
    editingModePort !== null;
  const anySaving =
    savingPort !== null ||
    savingTrunkPort !== null ||
    savingAccessVlanPort !== null ||
    savingModePort !== null;
  const busy: EditorsBusy = { bulkExecuting, anyEditing, anySaving };

  function onToggleSelect(port: Port, checked: boolean) {
    setSelectedPortsRaw((prev) => {
      const next = new Set(prev);
      if (checked) next.add(port.name);
      else next.delete(port.name);
      return next;
    });
  }

  function onSelectAllVisible(checked: boolean) {
    setSelectedPortsRaw((prev) => {
      const next = new Set(prev);
      if (checked) pageItems.forEach((p) => next.add(p.name));
      else pageItems.forEach((p) => next.delete(p.name));
      return next;
    });
  }

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

      <div className="mb-6 space-y-3">
        <div className="flex items-center gap-2">
          <label htmlFor="site-filter" className="text-sm text-gray-700">Site:</label>
          <select
            id="site-filter"
            value={siteFilter}
            onChange={(e) => setSiteFilter(e.target.value)}
            className={SELECT_CLS}
          >
            <option value="">All Sites</option>
            {siteList.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>

          <label htmlFor="device-select" className="text-sm text-gray-700 ml-3">Device:</label>
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

          {canEdit && <BulkActionToolbar bulk={bulk} />}

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
              <PortTable
                pageItems={pageItems}
                canEdit={canEdit}
                selectedPorts={selectedPorts}
                onToggleSelect={onToggleSelect}
                onSelectAllVisible={onSelectAllVisible}
                allPageSelected={allPageSelected}
                somePageSelected={somePageSelected}
                busy={busy}
                sortDir={sortDir}
                onToggleSort={() => setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))}
                description={description}
                admin={adminToggler}
                mode={modeEditor}
                accessVlan={accessVlanEditor}
                trunkVlans={trunkVlansEditor}
              />

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
