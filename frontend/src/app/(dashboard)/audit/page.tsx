'use client';

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter, useSearchParams, usePathname } from 'next/navigation';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getAuditLogs, getSites } from '@/services/api';
import type { AuditLog } from '@/types/audit';
import type { Site } from '@/types/site';

// ── Constants ─────────────────────────────────────────────────────────────────

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;
const DEFAULT_PAGE_SIZE = 25;

type DateRange = 'today' | '7d' | '30d' | 'all';

const SELECT_CLS =
  'px-2 py-1.5 text-sm border border-gray-300 rounded-md bg-white text-gray-700 focus:outline-none focus:ring-1 focus:ring-blue-400';
const INPUT_CLS =
  'px-2 py-1.5 text-sm border border-gray-300 rounded-md bg-white text-gray-700 placeholder-gray-400 focus:outline-none focus:ring-1 focus:ring-blue-400';

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.message ?? fallback;
}

function formatTimestamp(ts: string): string {
  return new Date(ts).toLocaleString();
}

function statusClass(status: string): string {
  if (status === 'success' || status === 'completed') return 'text-green-600';
  if (status === 'failed' || status === 'error' || status === 'failure') return 'text-red-600';
  if (status === 'pending' || status === 'running') return 'text-amber-600';
  return 'text-gray-600';
}

function startOfDayMs(offsetDays = 0): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() - offsetDays * 86_400_000;
}

function dateRangeToFromDate(range: DateRange): string | undefined {
  if (range === 'all') return undefined;
  const days = range === 'today' ? 0 : range === '7d' ? 7 : 30;
  return new Date(startOfDayMs(days)).toISOString();
}

function parseInt10(value: string | null, fallback: number, min = 1, max = Infinity): number {
  if (value == null) return fallback;
  const n = Number.parseInt(value, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

// ── URL state hook ────────────────────────────────────────────────────────────

interface AuditQueryState {
  page: number;
  pageSize: number;
  user: string;
  action: string;
  resource: string;
  status: string;
  device: string;
  site: string;  // empty = All Sites
  dateRange: DateRange;
}

function readQueryState(params: URLSearchParams): AuditQueryState {
  const rawSize = parseInt10(params.get('page_size'), DEFAULT_PAGE_SIZE, 1, 1000);
  const pageSize = (PAGE_SIZE_OPTIONS as readonly number[]).includes(rawSize)
    ? rawSize
    : DEFAULT_PAGE_SIZE;
  const range = params.get('range');
  const dateRange: DateRange =
    range === 'today' || range === '7d' || range === '30d' || range === 'all'
      ? range
      : 'all';
  return {
    page: parseInt10(params.get('page'), 1, 1, 1_000_000),
    pageSize,
    user: params.get('user') ?? '',
    action: params.get('action') ?? '',
    resource: params.get('resource') ?? '',
    status: params.get('status') ?? '',
    device: params.get('device') ?? '',
    site: params.get('site') ?? '',
    dateRange,
  };
}

function buildQueryString(state: Partial<AuditQueryState>): string {
  const params = new URLSearchParams();
  if (state.page != null && state.page !== 1) params.set('page', String(state.page));
  if (state.pageSize != null && state.pageSize !== DEFAULT_PAGE_SIZE) {
    params.set('page_size', String(state.pageSize));
  }
  if (state.user) params.set('user', state.user);
  if (state.action) params.set('action', state.action);
  if (state.resource) params.set('resource', state.resource);
  if (state.status) params.set('status', state.status);
  if (state.device) params.set('device', state.device);
  if (state.site) params.set('site', state.site);
  if (state.dateRange && state.dateRange !== 'all') params.set('range', state.dateRange);
  const s = params.toString();
  return s ? `?${s}` : '';
}

// ── Pagination ────────────────────────────────────────────────────────────────

interface PaginationProps {
  page: number;
  totalPages: number;
  totalItems: number;
  pageSize: number;
  onPrev: () => void;
  onNext: () => void;
  onGoTo: (p: number) => void;
}

function Pagination({ page, totalPages, totalItems, pageSize, onPrev, onNext, onGoTo }: PaginationProps) {
  const [inputVal, setInputVal] = useState(String(page));
  const [prevPage, setPrevPage] = useState(page);
  if (page !== prevPage) {
    setPrevPage(page);
    setInputVal(String(page));
  }

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

  if (totalPages <= 1) {
    return (
      <div className="flex items-center justify-between mt-4 text-sm">
        <span className="text-xs text-gray-400">
          {firstItem}–{lastItem} of {totalItems}
        </span>
      </div>
    );
  }

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
            onChange={e => setInputVal(e.target.value)}
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

// ── Filter bar ────────────────────────────────────────────────────────────────

interface FilterBarProps {
  draftUser: string;
  setDraftUser: (v: string) => void;
  draftAction: string;
  setDraftAction: (v: string) => void;
  draftDevice: string;
  setDraftDevice: (v: string) => void;
  resource: string;
  setResource: (v: string) => void;
  status: string;
  setStatus: (v: string) => void;
  site: string;
  setSite: (v: string) => void;
  sites: Site[];
  dateRange: DateRange;
  setDateRange: (v: DateRange) => void;
  pageSize: number;
  setPageSize: (v: number) => void;
  hasActiveFilters: boolean;
  onApplyText: () => void;
  onClearFilters: () => void;
  resources: string[];
  statuses: string[];
}

function FilterBar(props: FilterBarProps) {
  const {
    draftUser,
    setDraftUser,
    draftAction,
    setDraftAction,
    draftDevice,
    setDraftDevice,
    resource,
    setResource,
    status,
    setStatus,
    site,
    setSite,
    sites,
    dateRange,
    setDateRange,
    pageSize,
    setPageSize,
    hasActiveFilters,
    onApplyText,
    onClearFilters,
    resources,
    statuses,
  } = props;

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') onApplyText();
  }

  return (
    <div className="flex flex-wrap items-center gap-2 mb-3">
      <input
        type="text"
        value={draftUser}
        onChange={e => setDraftUser(e.target.value)}
        onBlur={onApplyText}
        onKeyDown={onKeyDown}
        placeholder="User"
        className={`${INPUT_CLS} w-28`}
      />

      <input
        type="text"
        value={draftAction}
        onChange={e => setDraftAction(e.target.value)}
        onBlur={onApplyText}
        onKeyDown={onKeyDown}
        placeholder="Action"
        className={`${INPUT_CLS} w-36`}
      />

      <select value={resource} onChange={e => setResource(e.target.value)} className={SELECT_CLS}>
        <option value="">All resources</option>
        {resources.map(r => (
          <option key={r} value={r}>{r}</option>
        ))}
      </select>

      <select value={status} onChange={e => setStatus(e.target.value)} className={SELECT_CLS}>
        <option value="">All statuses</option>
        {statuses.map(s => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>

      <input
        type="text"
        value={draftDevice}
        onChange={e => setDraftDevice(e.target.value)}
        onBlur={onApplyText}
        onKeyDown={onKeyDown}
        placeholder="Device"
        className={`${INPUT_CLS} w-32`}
      />

      <select value={site} onChange={e => setSite(e.target.value)} className={SELECT_CLS}>
        <option value="">All Sites</option>
        {sites.map(s => (
          <option key={s.id} value={s.id}>{s.name}</option>
        ))}
      </select>

      <select value={dateRange} onChange={e => setDateRange(e.target.value as DateRange)} className={SELECT_CLS}>
        <option value="all">All time</option>
        <option value="today">Today</option>
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
      </select>

      {hasActiveFilters && (
        <button onClick={onClearFilters} className="text-xs text-gray-500 hover:text-gray-700 underline px-1">
          Clear
        </button>
      )}

      <div className="ml-auto flex items-center gap-1.5">
        <span className="text-xs text-gray-400 whitespace-nowrap">Per page:</span>
        <select value={pageSize} onChange={e => setPageSize(Number(e.target.value))} className={SELECT_CLS}>
          {PAGE_SIZE_OPTIONS.map(n => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </div>
    </div>
  );
}

// ── Export helpers (frontend-only generation) ────────────────────────────────

const EXPORT_COLUMNS = [
  'id',
  'timestamp',
  'user',
  'action',
  'resource',
  'resource_id',
  'status',
  'device',
  'job_id',
  'request_id',
  'parent_audit_id',
  'details',
] as const;

const EXPORT_FETCH_CHUNK = 1000;
const EXPORT_MAX_ROWS = 100_000;

type ExportFormat = 'csv' | 'json';
type ExportScope = 'page' | 'all';

function csvEscape(value: unknown): string {
  if (value == null) return '';
  const raw = typeof value === 'string' ? value : JSON.stringify(value);
  if (/[",\r\n]/.test(raw)) return `"${raw.replace(/"/g, '""')}"`;
  return raw;
}

function toCsv(rows: AuditLog[]): string {
  const header = EXPORT_COLUMNS.join(',');
  const body = rows.map(r =>
    EXPORT_COLUMNS.map(c => csvEscape((r as unknown as Record<string, unknown>)[c])).join(','),
  );
  // UTF-8 BOM + CRLF — best compatibility with Excel and LibreOffice.
  return '﻿' + [header, ...body].join('\r\n') + '\r\n';
}

function toJsonPretty(rows: AuditLog[]): string {
  return JSON.stringify(rows, null, 2) + '\n';
}

function triggerDownload(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 100);
}

function sanitizeToken(s: string): string {
  return s
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 24);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function buildExportFilename(opts: {
  scope: ExportScope;
  state: AuditQueryState;
  ext: ExportFormat;
  hasFilters: boolean;
}): string {
  const { scope, state, ext, hasFilters } = opts;
  const tokens: string[] = ['audit_logs'];
  if (hasFilters) tokens.push('filtered');

  const filterTokens = [state.status, state.action, state.resource, state.user, state.device]
    .filter(Boolean)
    .map(sanitizeToken)
    .filter(Boolean)
    .slice(0, 3);
  tokens.push(...filterTokens);

  if (scope === 'page') tokens.push(`page${state.page}`);
  tokens.push(todayIso());
  return `${tokens.join('_')}.${ext}`;
}

type AuditQueryFilters = Omit<Parameters<typeof getAuditLogs>[0] & object, 'page' | 'page_size' | 'skip' | 'limit'>;

async function fetchAllFiltered(
  filters: AuditQueryFilters,
): Promise<{ rows: AuditLog[]; truncated: boolean }> {
  const collected: AuditLog[] = [];
  let skip = 0;
  let truncated = false;
  while (true) {
    const { items, total } = await getAuditLogs({ ...filters, skip, limit: EXPORT_FETCH_CHUNK });
    collected.push(...items);
    if (items.length < EXPORT_FETCH_CHUNK) break;
    if (total > 0 && collected.length >= total) break;
    if (collected.length >= EXPORT_MAX_ROWS) {
      truncated = total === 0 || total > EXPORT_MAX_ROWS;
      break;
    }
    skip += EXPORT_FETCH_CHUNK;
  }
  return { rows: collected.slice(0, EXPORT_MAX_ROWS), truncated };
}

// ── Export menu ──────────────────────────────────────────────────────────────

interface ExportMenuProps {
  onExport: (format: ExportFormat, scope: ExportScope) => void;
  busy: boolean;
  pageCount: number;
  totalCount: number;
}

function ExportMenu({ onExport, busy, pageCount, totalCount }: ExportMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const disabled = busy || (pageCount === 0 && totalCount === 0);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  function choose(format: ExportFormat, scope: ExportScope) {
    setOpen(false);
    onExport(format, scope);
  }

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {busy ? 'Exporting…' : 'Export ▾'}
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-1 w-64 rounded-md border border-gray-200 bg-white shadow-lg text-sm z-10"
        >
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-gray-400 border-b border-gray-100">
            Current page ({pageCount})
          </div>
          <button
            role="menuitem"
            onClick={() => choose('csv', 'page')}
            disabled={pageCount === 0}
            className="w-full text-left px-3 py-2 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Export current page (CSV)
          </button>
          <button
            role="menuitem"
            onClick={() => choose('json', 'page')}
            disabled={pageCount === 0}
            className="w-full text-left px-3 py-2 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Export current page (JSON)
          </button>
          <div className="px-3 py-1.5 text-[10px] uppercase tracking-wide text-gray-400 border-y border-gray-100">
            All filtered results ({totalCount.toLocaleString()})
          </div>
          <button
            role="menuitem"
            onClick={() => choose('csv', 'all')}
            disabled={totalCount === 0}
            className="w-full text-left px-3 py-2 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Export all filtered results (CSV)
          </button>
          <button
            role="menuitem"
            onClick={() => choose('json', 'all')}
            disabled={totalCount === 0}
            className="w-full text-left px-3 py-2 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Export all filtered results (JSON)
          </button>
        </div>
      )}
    </div>
  );
}

// ── Main content (uses useSearchParams — must be inside Suspense) ────────────

const KNOWN_RESOURCES = ['vlan', 'job', 'auth', 'device', 'user', 'audit_log', 'request'];
const KNOWN_STATUSES = ['success', 'pending', 'completed', 'failed', 'failure', 'cancelled', 'running'];

function AuditPageContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const state = useMemo(() => readQueryState(new URLSearchParams(searchParams.toString())), [searchParams]);

  // Local drafts for free-text fields so we don't fire a request on every keystroke.
  // Synced from URL state during render (back/forward, refresh, deep link) without an effect.
  const [draftUser, setDraftUser] = useState(state.user);
  const [draftAction, setDraftAction] = useState(state.action);
  const [draftDevice, setDraftDevice] = useState(state.device);
  const [syncedFrom, setSyncedFrom] = useState({ user: state.user, action: state.action, device: state.device });
  if (
    state.user !== syncedFrom.user ||
    state.action !== syncedFrom.action ||
    state.device !== syncedFrom.device
  ) {
    setSyncedFrom({ user: state.user, action: state.action, device: state.device });
    setDraftUser(state.user);
    setDraftAction(state.action);
    setDraftDevice(state.device);
  }

  const updateState = useCallback((patch: Partial<AuditQueryState>) => {
    const next = { ...state, ...patch };
    // changing anything other than the page must reset to page 1
    const filtersChanged =
      patch.user !== undefined ||
      patch.action !== undefined ||
      patch.resource !== undefined ||
      patch.status !== undefined ||
      patch.device !== undefined ||
      patch.site !== undefined ||
      patch.dateRange !== undefined ||
      patch.pageSize !== undefined;
    if (filtersChanged && patch.page === undefined) next.page = 1;
    router.replace(`${pathname}${buildQueryString(next)}`, { scroll: false });
  }, [state, router, pathname]);

  const applyTextFilters = useCallback(() => {
    if (draftUser === state.user && draftAction === state.action && draftDevice === state.device) return;
    updateState({ user: draftUser, action: draftAction, device: draftDevice });
  }, [draftUser, draftAction, draftDevice, state.user, state.action, state.device, updateState]);

  const clearFilters = useCallback(() => {
    setDraftUser('');
    setDraftAction('');
    setDraftDevice('');
    updateState({
      user: '',
      action: '',
      resource: '',
      status: '',
      device: '',
      site: '',
      dateRange: 'all',
    });
  }, [updateState]);

  const queryParams = useMemo(() => ({
    page: state.page,
    page_size: state.pageSize,
    user: state.user || undefined,
    action: state.action || undefined,
    resource: state.resource || undefined,
    status: state.status || undefined,
    device_id: state.device || undefined,
    site_id: state.site ? Number(state.site) : undefined,
    from_date: dateRangeToFromDate(state.dateRange),
  }), [state]);

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['audit-logs', queryParams],
    queryFn: () => getAuditLogs(queryParams),
    placeholderData: keepPreviousData,
  });

  const items: AuditLog[] = useMemo(() => data?.items ?? [], [data?.items]);
  const total: number = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / state.pageSize));

  // If the current page exceeds the available pages after a filter change, clamp it.
  useEffect(() => {
    if (!isLoading && total > 0 && state.page > totalPages) {
      updateState({ page: totalPages });
    }
  }, [isLoading, total, totalPages, state.page, updateState]);

  // Build select options from current page items + a known baseline so the lists are stable.
  const resources = useMemo(() => {
    const s = new Set<string>(KNOWN_RESOURCES);
    items.forEach(i => { if (i.resource) s.add(i.resource); });
    return Array.from(s).sort();
  }, [items]);

  const statuses = useMemo(() => {
    const s = new Set<string>(KNOWN_STATUSES);
    items.forEach(i => { if (i.status) s.add(i.status); });
    return Array.from(s).sort();
  }, [items]);

  const hasActiveFilters =
    !!state.user ||
    !!state.action ||
    !!state.resource ||
    !!state.status ||
    !!state.device ||
    !!state.site ||
    state.dateRange !== 'all';

  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportInfo, setExportInfo] = useState<{ level: 'success' | 'warning'; message: string } | null>(null);

  // Auto-dismiss the success / truncation banner after a short delay.
  useEffect(() => {
    if (!exportInfo) return;
    const t = setTimeout(() => setExportInfo(null), 6000);
    return () => clearTimeout(t);
  }, [exportInfo]);

  const handleExport = useCallback(
    async (format: ExportFormat, scope: ExportScope) => {
      setExporting(true);
      setExportError(null);
      setExportInfo(null);
      try {
        let rows: AuditLog[];
        let truncated = false;
        if (scope === 'page') {
          rows = items;
        } else {
          const result = await fetchAllFiltered({
            user: state.user || undefined,
            action: state.action || undefined,
            resource: state.resource || undefined,
            status: state.status || undefined,
            device_id: state.device || undefined,
            site_id: state.site ? Number(state.site) : undefined,
            from_date: dateRangeToFromDate(state.dateRange),
          });
          rows = result.rows;
          truncated = result.truncated;
        }
        const content = format === 'csv' ? toCsv(rows) : toJsonPretty(rows);
        const filename = buildExportFilename({ scope, state, ext: format, hasFilters: hasActiveFilters });
        const mime = format === 'csv' ? 'text/csv' : 'application/json';
        triggerDownload(content, filename, mime);

        setExportInfo(
          truncated
            ? {
                level: 'warning',
                message: `Export truncated to ${EXPORT_MAX_ROWS.toLocaleString()} rows (safety limit reached)`,
              }
            : {
                level: 'success',
                message: `Export completed (${rows.length.toLocaleString()} rows)`,
              },
        );
      } catch (e) {
        setExportError(extractMessage(e, 'Export failed'));
      } finally {
        setExporting(false);
      }
    },
    [items, state, hasActiveFilters],
  );

  return (
    // Block 1 transitional wrapper — legacy layout keeps its light palette on a
    // white "canvas" so it stays readable under the new dark app shell. Block 6
    // will rewrite the whole page against the dark theme (light text, white
    // hover row) and drop this wrapper.
    <div className="bg-white text-gray-900 rounded-lg p-6 shadow">
      <PageHeader
        title="Audit Logs"
        actions={
          <div className="flex items-center gap-2">
            <ExportMenu
              onExport={handleExport}
              busy={exporting}
              pageCount={items.length}
              totalCount={total}
            />
            <button
              onClick={() => refetch()}
              disabled={isLoading || isFetching}
              className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isFetching ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>
        }
      />
      <p className="text-sm text-gray-500 mb-6">View system audit history</p>

      {exportError && (
        <div className="mb-3">
          <ErrorMessage error={exportError} />
        </div>
      )}

      {exportInfo && (
        <div
          role="status"
          className={`mb-3 px-3 py-2 text-sm rounded-md border ${
            exportInfo.level === 'warning'
              ? 'bg-amber-50 border-amber-200 text-amber-800'
              : 'bg-green-50 border-green-200 text-green-700'
          }`}
        >
          {exportInfo.message}
        </div>
      )}

      <FilterBar
        draftUser={draftUser}
        setDraftUser={setDraftUser}
        draftAction={draftAction}
        setDraftAction={setDraftAction}
        draftDevice={draftDevice}
        setDraftDevice={setDraftDevice}
        resource={state.resource}
        setResource={r => updateState({ resource: r })}
        status={state.status}
        setStatus={s => updateState({ status: s })}
        site={state.site}
        setSite={s => updateState({ site: s })}
        sites={siteList}
        dateRange={state.dateRange}
        setDateRange={r => updateState({ dateRange: r })}
        pageSize={state.pageSize}
        setPageSize={n => updateState({ pageSize: n })}
        hasActiveFilters={hasActiveFilters}
        onApplyText={applyTextFilters}
        onClearFilters={clearFilters}
        resources={resources}
        statuses={statuses}
      />

      {isLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : error ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(error, 'Failed to load audit logs')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : items.length === 0 ? (
        <div className="py-12 text-center">
          <p className="text-gray-400 text-sm">No audit logs found.</p>
          {hasActiveFilters && (
            <button onClick={clearFilters} className="mt-2 text-xs text-blue-600 hover:underline">
              Clear filters
            </button>
          )}
        </div>
      ) : (
        <>
          <p className="text-xs text-gray-400 mb-2">
            {total} total{hasActiveFilters && ' (filtered)'}
          </p>

          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50">
                <th className="text-left px-4 py-2 font-medium text-gray-700 whitespace-nowrap">Timestamp</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">User</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Action</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Resource Type</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Status</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Details</th>
              </tr>
            </thead>
            <tbody>
              {items.map(log => (
                <tr key={log.id} className="border-b border-gray-100 hover:bg-gray-50 align-top">
                  <td className="px-4 py-2 text-gray-600 whitespace-nowrap text-xs">
                    {formatTimestamp(log.timestamp)}
                  </td>
                  <td className="px-4 py-2 text-gray-900">{log.user}</td>
                  <td className="px-4 py-2 font-mono text-xs text-gray-900">{log.action}</td>
                  <td className="px-4 py-2 text-gray-600">{log.resource}</td>
                  <td className={`px-4 py-2 font-medium ${statusClass(log.status)}`}>
                    {log.status}
                  </td>
                  <td className="px-4 py-2">
                    <pre className="text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-2 max-w-xs overflow-auto max-h-32 whitespace-pre-wrap">
                      {JSON.stringify(log.details, null, 2)}
                    </pre>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <Pagination
            page={state.page}
            totalPages={totalPages}
            totalItems={total}
            pageSize={state.pageSize}
            onPrev={() => updateState({ page: Math.max(1, state.page - 1) })}
            onNext={() => updateState({ page: Math.min(totalPages, state.page + 1) })}
            onGoTo={p => updateState({ page: p })}
          />
        </>
      )}
    </div>
  );
}

// ── Page (system-admin gate + Suspense boundary for useSearchParams) ─────────

export default function AuditPage() {
  const { user } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (user && !user.is_system_admin) {
      router.push('/');
    }
  }, [user, router]);

  if (!user || !user.is_system_admin) return null;

  return (
    <Suspense
      fallback={
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      }
    >
      <AuditPageContent />
    </Suspense>
  );
}
