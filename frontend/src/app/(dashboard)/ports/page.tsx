'use client';

import { useState, useMemo, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getDevices, getPorts, getSites } from '@/services/api';
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

// ── Status pills ──────────────────────────────────────────────────────────────

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
  useEffect(() => {
    setInputVal(String(page));
  }, [page]);

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
  const [selectedDevice, setSelectedDevice] = useState<string>('');
  const [siteFilter, setSiteFilter] = useState<string>('');

  const [search, setSearch] = useState('');
  const [filterMode, setFilterMode] = useState<PortMode | ''>('');
  const [filterAdmin, setFilterAdmin] = useState<AdminFilter>('');
  const [filterOper, setFilterOper] = useState<OperFilter>('');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

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

  // Auto-select the first device once the list arrives so the table is never empty by default.
  useEffect(() => {
    if (!selectedDevice && filteredDevices.length > 0) {
      setSelectedDevice(filteredDevices[0].name);
    }
    // If the selected device is filtered out by a new site choice, fall back to first visible.
    if (selectedDevice && !filteredDevices.some((d) => d.name === selectedDevice)) {
      setSelectedDevice(filteredDevices[0]?.name ?? '');
    }
  }, [filteredDevices, selectedDevice]);

  // ── Port fetch ──
  const {
    data: portsResponse,
    isLoading: portsLoading,
    isFetching: portsFetching,
    error: portsError,
    refetch,
  } = useQuery<PortListResponse>({
    queryKey: ['ports', selectedDevice],
    queryFn: () => getPorts(selectedDevice),
    enabled: !!selectedDevice,
  });

  // Reset to page 1 whenever the data set or filters change.
  useEffect(() => {
    setPage(1);
  }, [selectedDevice, search, filterMode, filterAdmin, filterOper, pageSize]);

  // ── Apply filters + search client-side ──
  const filteredPorts = useMemo(() => {
    const all = portsResponse?.ports ?? [];
    const q = search.trim().toLowerCase();

    return all.filter((p) => {
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
  }, [portsResponse, search, filterMode, filterAdmin, filterOper]);

  const totalItems = filteredPorts.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize));
  const pageItems: Port[] = useMemo(() => {
    const start = (page - 1) * pageSize;
    return filteredPorts.slice(start, start + pageSize);
  }, [filteredPorts, page, pageSize]);

  const hasActiveFilters = !!search || !!filterMode || !!filterAdmin || !!filterOper;

  function clearFilters() {
    setSearch('');
    setFilterMode('');
    setFilterAdmin('');
    setFilterOper('');
  }

  // ── Render ──
  return (
    <div>
      <PageHeader
        title="Port Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={portsLoading || portsFetching || !selectedDevice}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {portsFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">
        View port inventory and state on managed devices. Read-only.
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
            value={selectedDevice}
            onChange={(e) => setSelectedDevice(e.target.value)}
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
      {!selectedDevice ? (
        <div className="py-12 text-center">
          <p className="text-sm text-gray-400">Select a device to view its ports.</p>
        </div>
      ) : portsLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : portsError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(portsError, 'Could not load ports')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : (
        <>
          <FilterBar
            search={search}
            setSearch={setSearch}
            filterMode={filterMode}
            setFilterMode={setFilterMode}
            filterAdmin={filterAdmin}
            setFilterAdmin={setFilterAdmin}
            filterOper={filterOper}
            setFilterOper={setFilterOper}
            pageSize={pageSize}
            setPageSize={setPageSize}
            hasActiveFilters={hasActiveFilters}
            onClear={clearFilters}
          />

          <p className="text-xs text-gray-400 mb-2">
            {portsResponse?.count ?? 0} total
            {hasActiveFilters && ` — ${totalItems} match the current filters`}
          </p>

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
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-sm">
                  <thead>
                    <tr className="border-b border-gray-200 bg-gray-50">
                      <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Interface</th>
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
                      <tr key={port.name} className="border-b border-gray-100 hover:bg-gray-50 transition-colors">
                        <td className="px-3 py-2 font-mono text-xs text-gray-900 whitespace-nowrap">
                          {port.name}
                        </td>
                        <td className="px-3 py-2 text-gray-900">{port.description ?? DASH}</td>
                        <td className="px-3 py-2"><AdminBadge value={port.admin_up} /></td>
                        <td className="px-3 py-2"><OperBadge value={port.operational_up} /></td>
                        <td className="px-3 py-2"><ModeBadge mode={port.mode} /></td>
                        <td className="px-3 py-2 text-gray-900">{port.access_vlan ?? DASH}</td>
                        <td className="px-3 py-2 text-gray-900 max-w-xs truncate" title={formatVlanList(port.allowed_vlans)}>
                          {formatVlanList(port.allowed_vlans)}
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
