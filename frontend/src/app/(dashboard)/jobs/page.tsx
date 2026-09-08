'use client';

import { useState, useMemo, useEffect } from 'react';
import { useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { StatusBadge } from '@/components/StatusBadge';
import { ElapsedTimer } from '@/components/ElapsedTimer';
import { JobDetailModal } from '@/components/JobDetailModal';
import { getJobs, getSites, extractMessage } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { ACTIVE_JOB_STATUSES } from '@/types/job';
import type { Job } from '@/types/job';
import type { Site } from '@/types/site';

// ── Types ─────────────────────────────────────────────────────────────────────

type SortKey = 'newest' | 'oldest' | 'duration' | 'failures_first';
type DateRange = 'today' | '7d' | '30d' | 'all';

const PAGE_SIZE_OPTIONS = [25, 50, 100] as const;

// ── Helpers ───────────────────────────────────────────────────────────────────

function getDurationMs(job: Job): number | null {
  if (!job.started_at || !job.finished_at) return null;
  return new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
}

function formatDurationMs(ms: number | null): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatDate(ts: string | null | undefined): string {
  if (!ts) return '—';
  return new Date(ts).toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function isToday(ts: string | null | undefined): boolean {
  if (!ts) return false;
  const d = new Date(ts);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function startOfDay(offsetDays = 0): number {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d.getTime() - offsetDays * 86_400_000;
}

// ── Summary bar ───────────────────────────────────────────────────────────────

function SummaryBar({ jobs, total }: { jobs: Job[]; total: number }) {
  const todayCount = jobs.filter(j => isToday(j.created_at)).length;
  const terminal = jobs.filter(j => j.status === 'completed' || j.status === 'failed');
  const successRate =
    terminal.length === 0
      ? null
      : Math.round((terminal.filter(j => j.status === 'completed').length / terminal.length) * 100);
  const failureCount = jobs.filter(j => j.status === 'failed').length;
  const rollbackCount = jobs.filter(j => j.rollback_performed).length;

  const stats: { label: string; value: string | number; warn: boolean }[] = [
    { label: 'Jobs today', value: todayCount, warn: false },
    { label: 'Success rate', value: successRate != null ? `${successRate}%` : '—', warn: successRate != null && successRate < 90 },
    { label: 'Failures', value: failureCount, warn: failureCount > 0 },
    { label: 'Rollbacks', value: rollbackCount, warn: rollbackCount > 0 },
  ];

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
      {stats.map(s => (
        <div key={s.label} className="bg-panel border border-panel-border rounded-md px-4 py-3">
          <div className={`text-2xl font-semibold tabular-nums ${s.warn ? 'text-red-600' : 'text-text'}`}>
            {s.value}
          </div>
          <div className="text-xs text-muted mt-0.5">{s.label}</div>
        </div>
      ))}
      {total > jobs.length && (
        <p className="col-span-2 sm:col-span-4 text-xs text-muted/70 -mt-1">
          Stats reflect this page only ({jobs.length} of {total} jobs loaded)
        </p>
      )}
    </div>
  );
}

// ── Shared select class ───────────────────────────────────────────────────────

const SELECT_CLS =
  'px-2 py-1.5 text-sm border border-panel-border rounded-md bg-panel text-text focus:outline-none focus:ring-1 focus:ring-info';

// ── Filter + sort bar ─────────────────────────────────────────────────────────

interface FilterBarProps {
  devices: string[];
  playbooks: string[];
  sites: Site[];
  filterSite: string;
  setFilterSite: (v: string) => void;
  filterDevice: string;
  setFilterDevice: (v: string) => void;
  filterStatus: string;
  setFilterStatus: (v: string) => void;
  filterPlaybook: string;
  setFilterPlaybook: (v: string) => void;
  filterDateRange: DateRange;
  setFilterDateRange: (v: DateRange) => void;
  sort: SortKey;
  setSort: (v: SortKey) => void;
  pageSize: number;
  setPageSize: (v: number) => void;
  hasActiveFilters: boolean;
  onClearFilters: () => void;
}

function FilterBar({
  devices,
  playbooks,
  sites,
  filterSite,
  setFilterSite,
  filterDevice,
  setFilterDevice,
  filterStatus,
  setFilterStatus,
  filterPlaybook,
  setFilterPlaybook,
  filterDateRange,
  setFilterDateRange,
  sort,
  setSort,
  pageSize,
  setPageSize,
  hasActiveFilters,
  onClearFilters,
}: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 mb-3">
      <select value={filterSite} onChange={e => setFilterSite(e.target.value)} className={SELECT_CLS}>
        <option value="">All Sites</option>
        {sites.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>

      <select value={filterDevice} onChange={e => setFilterDevice(e.target.value)} className={SELECT_CLS}>
        <option value="">All devices</option>
        {devices.map(d => <option key={d} value={d}>{d}</option>)}
      </select>

      <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)} className={SELECT_CLS}>
        <option value="">All statuses</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
        <option value="running">Running</option>
        <option value="pending">Queued</option>
        <option value="retrying">Retrying</option>
        <option value="cancelled">Cancelled</option>
        <option value="rollback_performed">Rollback executed</option>
      </select>

      {playbooks.length > 0 && (
        <select value={filterPlaybook} onChange={e => setFilterPlaybook(e.target.value)} className={SELECT_CLS}>
          <option value="">All actions</option>
          {playbooks.map(p => <option key={p} value={p}>{p}</option>)}
        </select>
      )}

      <select value={filterDateRange} onChange={e => setFilterDateRange(e.target.value as DateRange)} className={SELECT_CLS}>
        <option value="all">All time</option>
        <option value="today">Today</option>
        <option value="7d">Last 7 days</option>
        <option value="30d">Last 30 days</option>
      </select>

      {hasActiveFilters && (
        <button onClick={onClearFilters} className="text-xs text-muted hover:text-text underline px-1">
          Clear
        </button>
      )}

      <div className="ml-auto flex items-center gap-3">
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted/70 whitespace-nowrap">Per page:</span>
          <select value={pageSize} onChange={e => setPageSize(Number(e.target.value))} className={SELECT_CLS}>
            {PAGE_SIZE_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
          </select>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted/70 whitespace-nowrap">Sort:</span>
          <select value={sort} onChange={e => setSort(e.target.value as SortKey)} className={SELECT_CLS}>
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="duration">Longest duration</option>
            <option value="failures_first">Failures first</option>
          </select>
        </div>
      </div>
    </div>
  );
}

// ── Pagination controls ───────────────────────────────────────────────────────

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

  useEffect(() => { setInputVal(String(page)); }, [page]);

  const firstItem = (page - 1) * pageSize + 1;
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
      <span className="text-xs text-muted/70">
        {firstItem}–{lastItem} of {totalItems}
      </span>

      <div className="flex items-center gap-2">
        <button
          onClick={onPrev}
          disabled={page <= 1}
          className="px-2.5 py-1 text-xs border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          ← Prev
        </button>

        <div className="flex items-center gap-1 text-xs text-muted">
          <span>Page</span>
          <input
            type="text"
            value={inputVal}
            onChange={e => setInputVal(e.target.value)}
            onKeyDown={handleKey}
            onBlur={handleBlur}
            className="w-10 px-1.5 py-0.5 text-center border border-panel-border rounded focus:outline-none focus:ring-1 focus:ring-info"
            aria-label="Page number"
          />
          <span>of {totalPages}</span>
        </div>

        <button
          onClick={onNext}
          disabled={page >= totalPages}
          className="px-2.5 py-1 text-xs border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Next →
        </button>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function JobsPage() {
  // Seed filters from URL — dashboards deep-link here as ?site_id=…&range=7d etc.
  const searchParams = useSearchParams();
  const [filterSite, setFilterSite] = useState(() => searchParams.get('site_id') ?? '');
  const [filterDevice, setFilterDevice] = useState(() => searchParams.get('device') ?? '');
  const [filterStatus, setFilterStatus] = useState(() => searchParams.get('status') ?? '');
  const [filterPlaybook, setFilterPlaybook] = useState('');
  const [filterDateRange, setFilterDateRange] = useState<DateRange>(() => {
    const r = searchParams.get('range');
    return r === 'today' || r === '7d' || r === '30d' || r === 'all' ? r : 'all';
  });
  const [sort, setSort] = useState<SortKey>('newest');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const { jobs: trackedJobs } = useJobNotifications();
  const trackedIds = new Set(trackedJobs.map(n => n.jobId));

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  // reset to page 1 when server-side params change
  useEffect(() => { setPage(1); }, [filterStatus, filterDevice, filterSite, pageSize]);

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['jobs', page, pageSize, filterStatus, filterDevice, filterSite],
    queryFn: () => getJobs({
      page,
      page_size: pageSize,
      status: filterStatus || undefined,
      device: filterDevice || undefined,
      site_id: filterSite ? Number(filterSite) : undefined,
    }),
    refetchInterval: (query) => {
      const items = (query.state.data as { items: Job[] } | undefined)?.items ?? [];
      const hasActive = items.some(j => (ACTIVE_JOB_STATUSES as string[]).includes(j.status));
      return hasActive ? 2500 : false;
    },
  });

  const pageItems: Job[] = data?.items ?? [];
  const serverTotal: number = data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(serverTotal / pageSize));

  // client-side filters applied on top of the server page
  const displayItems = useMemo(() => {
    const cutoffs: Record<DateRange, number | null> = {
      today: startOfDay(),
      '7d': startOfDay(7),
      '30d': startOfDay(30),
      all: null,
    };

    let jobs = pageItems;
    if (filterPlaybook) jobs = jobs.filter(j => j.playbook === filterPlaybook);
    const cut = cutoffs[filterDateRange];
    if (cut != null) jobs = jobs.filter(j => j.created_at != null && new Date(j.created_at).getTime() >= cut);

    const result = [...jobs];
    if (sort === 'newest') {
      result.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
    } else if (sort === 'oldest') {
      result.sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
    } else if (sort === 'duration') {
      result.sort((a, b) => (getDurationMs(b) ?? -1) - (getDurationMs(a) ?? -1));
    } else if (sort === 'failures_first') {
      const rank = (j: Job): number => {
        if (j.status === 'failed') return 0;
        if (j.status === 'retrying') return 1;
        if (j.rollback_performed) return 2;
        return 3;
      };
      result.sort((a, b) => rank(a) - rank(b));
    }
    return result;
  }, [pageItems, filterPlaybook, filterDateRange, sort]);

  const uniqueDevices = useMemo(() => {
    const s = new Set(pageItems.map(j => j.device).filter(Boolean) as string[]);
    return Array.from(s).sort();
  }, [pageItems]);

  const uniquePlaybooks = useMemo(() => {
    const s = new Set(pageItems.map(j => j.playbook).filter(Boolean) as string[]);
    return Array.from(s).sort();
  }, [pageItems]);

  const hasActiveFilters =
    !!filterSite || !!filterDevice || !!filterStatus || !!filterPlaybook || filterDateRange !== 'all';

  function clearFilters() {
    setFilterSite('');
    setFilterDevice('');
    setFilterStatus('');
    setFilterPlaybook('');
    setFilterDateRange('all');
  }

  return (
    <div>
      <PageHeader
        title="Execution History"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching}
            className="px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-muted mb-6">Network automation job execution history</p>

      {isLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : error ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(error, 'Could not load jobs')} />
          <button onClick={() => refetch()} className="mt-3 px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60">
            Retry
          </button>
        </div>
      ) : (
        <>
          <SummaryBar jobs={pageItems} total={serverTotal} />

          <FilterBar
            devices={uniqueDevices}
            playbooks={uniquePlaybooks}
            sites={siteList}
            filterSite={filterSite}
            setFilterSite={setFilterSite}
            filterDevice={filterDevice}
            setFilterDevice={setFilterDevice}
            filterStatus={filterStatus}
            setFilterStatus={setFilterStatus}
            filterPlaybook={filterPlaybook}
            setFilterPlaybook={setFilterPlaybook}
            filterDateRange={filterDateRange}
            setFilterDateRange={setFilterDateRange}
            sort={sort}
            setSort={setSort}
            pageSize={pageSize}
            setPageSize={setPageSize}
            hasActiveFilters={hasActiveFilters}
            onClearFilters={clearFilters}
          />

          {displayItems.length === 0 ? (
            <div className="py-12 text-center">
              <p className="text-muted/70 text-sm">No jobs match the current filters.</p>
              {hasActiveFilters && (
                <button onClick={clearFilters} className="mt-2 text-xs text-info hover:underline">
                  Clear filters
                </button>
              )}
            </div>
          ) : (
            <>
              <p className="text-xs text-muted/70 mb-2">
                {serverTotal} total
                {hasActiveFilters && ` — showing ${displayItems.length} on this page`}
              </p>

              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="border-b border-panel-border bg-panel-elev/60">
                    <th className="text-left px-4 py-2 font-medium text-text">Job ID</th>
                    <th className="text-left px-4 py-2 font-medium text-text">Action</th>
                    <th className="text-left px-4 py-2 font-medium text-text">Device</th>
                    <th className="text-left px-4 py-2 font-medium text-text">Status</th>
                    <th className="text-left px-4 py-2 font-medium text-text">Duration</th>
                    <th className="text-left px-4 py-2 font-medium text-text">Created</th>
                    <th className="px-4 py-2" />
                  </tr>
                </thead>
                <tbody>
                  {displayItems.map(job => {
                    const isActive = (ACTIVE_JOB_STATUSES as string[]).includes(job.status);
                    const durationMs = getDurationMs(job);

                    return (
                      <tr key={job.job_id} className="border-b border-panel-border hover:bg-panel-elev/60 transition-colors">
                        <td className="px-4 py-2.5 font-mono text-xs text-muted">
                          {job.job_id.slice(0, 8)}…
                          {trackedIds.has(job.job_id) && (
                            <span className="ml-1.5 px-1 py-0.5 rounded bg-info/20 text-info font-sans font-medium">
                              tracked
                            </span>
                          )}
                        </td>

                        <td className="px-4 py-2.5 text-text">{job.playbook ?? '—'}</td>

                        <td className="px-4 py-2.5 text-text">{job.device ?? '—'}</td>

                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <StatusBadge status={job.status} />
                            {job.retry_count > 0 && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-warning/20 text-warning">
                                ↺ retried
                              </span>
                            )}
                            {job.rollback_performed && (
                              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-warning/20 text-warning">
                                ↩ rollback
                              </span>
                            )}
                          </div>
                          {job.status === 'failed' && (job.error_summary ?? job.error ?? job.last_error) && (
                            <p
                              className="mt-0.5 text-xs text-danger truncate max-w-[220px]"
                              title={job.error ?? job.last_error ?? undefined}
                            >
                              {job.error_summary ?? job.error ?? job.last_error}
                            </p>
                          )}
                        </td>

                        <td className="px-4 py-2.5 text-muted tabular-nums text-sm">
                          {isActive
                            ? <ElapsedTimer startedAt={job.started_at} className="text-xs text-warning" />
                            : formatDurationMs(durationMs)}
                        </td>

                        <td className="px-4 py-2.5 text-muted text-xs whitespace-nowrap">
                          {formatDate(job.created_at)}
                        </td>

                        <td className="px-4 py-2.5 text-right">
                          <button
                            onClick={() => setSelectedJobId(job.job_id)}
                            className="text-xs text-info hover:underline whitespace-nowrap"
                          >
                            Details
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              <Pagination
                page={page}
                totalPages={totalPages}
                totalItems={serverTotal}
                pageSize={pageSize}
                onPrev={() => setPage(p => Math.max(1, p - 1))}
                onNext={() => setPage(p => Math.min(totalPages, p + 1))}
                onGoTo={setPage}
              />
            </>
          )}
        </>
      )}

      {selectedJobId && (
        <JobDetailModal jobId={selectedJobId} onClose={() => setSelectedJobId(null)} />
      )}
    </div>
  );
}
