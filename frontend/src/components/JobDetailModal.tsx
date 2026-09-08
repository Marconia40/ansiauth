'use client';

import { useState, useEffect, useCallback } from 'react';
import { getJob, getGroupJob } from '@/services/api';
import { StatusBadge } from '@/components/StatusBadge';
import { ElapsedTimer } from '@/components/ElapsedTimer';
import { ACTIVE_JOB_STATUSES } from '@/types/job';
import type { Job, GroupJob, GroupJobDeviceResult } from '@/types/job';

// Mirrors JobNotificationContext's JOB_POLL_INTERVAL_MS -- kept as its own
// constant since this modal polls independently of the toast context (it
// must keep working even after the underlying toast has already been
// dismissed).
const JOB_DETAIL_POLL_INTERVAL_MS = 2500;

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatMs(ms: number | null | undefined): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function deriveDurationMs(startedAt: string | null, finishedAt: string | null): number | null {
  if (!startedAt || !finishedAt) return null;
  return Math.round(new Date(finishedAt).getTime() - new Date(startedAt).getTime());
}

// The backend dumps the FULL request dataclass as `parameters` (every
// field the resource type can carry, e.g. every GlobalConfig field --
// hostname, snmp_config, routes, acls...), not just what this particular
// request actually set. Rendering that verbatim buries the 1-2 fields that
// matter under a wall of "field: null". Recurses because the batch shape
// (`encolar_lote()` -> `{lote: [asdict(r), ...]}`, used by SVI's batch
// editor) nests the same problem a level down -- a shallow filter would
// clean the top level and leave the wall of nulls inside `lote[0]`.
function stripEmpty(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(stripEmpty).filter((v) => v !== undefined);
  }
  if (value !== null && typeof value === 'object') {
    const cleaned = Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .map(([k, v]) => [k, stripEmpty(v)] as const)
        .filter(([, v]) => v !== null && v !== undefined && v !== ''),
    );
    return Object.keys(cleaned).length > 0 ? cleaned : undefined;
  }
  return value;
}

function meaningfulParameters(parameters: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!parameters) return null;
  const cleaned = stripEmpty(parameters) as Record<string, unknown> | undefined;
  return cleaned && Object.keys(cleaned).length > 0 ? cleaned : null;
}

// ── Building blocks ───────────────────────────────────────────────────────────

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-xs font-semibold uppercase tracking-wide text-muted/70 mb-2 mt-4 first:mt-0">
      {children}
    </div>
  );
}

function Field({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-3 py-1 text-sm border-b border-gray-50 last:border-0">
      <span className="w-32 flex-shrink-0 text-muted">{label}</span>
      <span className={`flex-1 break-all ${mono ? 'font-mono text-xs' : 'text-text'}`}>
        {children ?? <span className="text-muted/50">—</span>}
      </span>
    </div>
  );
}

function JsonBlock({ data }: { data: unknown }) {
  if (data == null) return <span className="text-muted/50 text-xs">null</span>;
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  return (
    <pre className="mt-1 text-xs bg-panel-elev/60 border border-panel-border rounded p-2 overflow-auto max-h-36 text-text whitespace-pre-wrap break-all">
      {text}
    </pre>
  );
}

function RollbackRow({ performed, success }: { performed: boolean; success: boolean | null }) {
  if (!performed) return <Field label="Rollback">not performed</Field>;
  return (
    <Field label="Rollback">
      <span>performed — </span>
      {success === true && <span className="text-green-600">succeeded</span>}
      {success === false && <span className="text-red-600">failed</span>}
      {success === null && <span className="text-muted/70">unverified</span>}
    </Field>
  );
}

// ── Single job view ───────────────────────────────────────────────────────────

function SingleJobView({ job, backLabel, onBack }: { job: Job; backLabel?: string; onBack?: () => void }) {
  const durationMs = job.execution_summary?.duration_ms ?? deriveDurationMs(job.started_at, job.finished_at);
  const attempts = job.execution_summary?.attempts;
  const isActive = (ACTIVE_JOB_STATUSES as string[]).includes(job.status);
  const hasPreState = job.pre_state != null;
  const hasResult = job.result != null;
  const hasError = !!(job.error || job.last_error);

  return (
    <div>
      {onBack && (
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-xs text-info hover:underline mb-3"
        >
          ← {backLabel ?? 'Back'}
        </button>
      )}

      <SectionHeader>Overview</SectionHeader>
      <Field label="Status">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusBadge status={job.status} />
          {job.rollback_performed && job.status !== 'rollback_performed' && (
            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-warning/10 text-warning">
              Rollback executed
            </span>
          )}
          {isActive && <ElapsedTimer startedAt={job.started_at} />}
          {!isActive && durationMs != null && (
            <span className="text-xs text-muted/70">{formatMs(durationMs)}</span>
          )}
        </div>
      </Field>
      {hasError && job.status === 'failed' && (
        <Field label="Error">
          <span className="text-red-600 text-xs truncate block max-w-full">
            {job.error ?? job.last_error}
          </span>
        </Field>
      )}
      <Field label="Device">{job.device}</Field>
      {job.parameters_summary && (
        <Field label="Change">{job.parameters_summary}</Field>
      )}
      <Field label="Job ID" mono>{job.job_id}</Field>
      {job.group_job_id && (
        <Field label="Group Job" mono>{job.group_job_id}</Field>
      )}

      <SectionHeader>Execution</SectionHeader>
      <Field label="Duration">
        {isActive
          ? <ElapsedTimer startedAt={job.started_at} />
          : formatMs(durationMs)}
      </Field>
      {attempts != null && (
        <Field label="Attempts">{attempts} / {job.max_retries + 1}</Field>
      )}
      {attempts == null && job.retry_count > 0 && (
        <Field label="Retries">{job.retry_count} / {job.max_retries}</Field>
      )}
      <RollbackRow
        performed={job.rollback_performed}
        success={job.rollback_success}
      />
      {job.current_step && (
        <Field label="Current step" mono>{job.current_step}</Field>
      )}

      <SectionHeader>Timing</SectionHeader>
      <Field label="Created">{formatDateTime(job.created_at)}</Field>
      <Field label="Started">{formatDateTime(job.started_at)}</Field>
      <Field label="Finished">{formatDateTime(job.finished_at)}</Field>

      {hasPreState && (
        <>
          <SectionHeader>Pre-state</SectionHeader>
          <JsonBlock data={job.pre_state} />
        </>
      )}

      {hasResult && (
        <>
          <SectionHeader>Result</SectionHeader>
          <JsonBlock data={job.result} />
        </>
      )}

      {hasError && job.status !== 'failed' && (
        <>
          <SectionHeader>Error</SectionHeader>
          <div className="text-sm text-danger bg-danger/10 border border-danger/40 rounded p-2 break-all">
            {job.error ?? job.last_error}
          </div>
        </>
      )}
      {hasError && job.status === 'failed' && (
        <>
          <SectionHeader>Full Error</SectionHeader>
          <div className="text-sm text-danger bg-danger/10 border border-danger/40 rounded p-2 break-all">
            {job.error ?? job.last_error}
          </div>
        </>
      )}
    </div>
  );
}

// ── Device result row in group view ──────────────────────────────────────────

function deviceIcon(status: string): { icon: string; className: string } {
  if (status === 'completed') return { icon: '✓', className: 'text-green-600' };
  if (status === 'failed' || status === 'cancelled') return { icon: '✗', className: 'text-red-600' };
  if (status === 'rollback_performed') return { icon: '↩', className: 'text-warning' };
  if (status === 'running' || status === 'retrying') return { icon: '↻', className: 'text-amber-600' };
  return { icon: '·', className: 'text-muted/70' };
}

function DeviceRow({
  dr,
  onDrillDown,
}: {
  dr: GroupJobDeviceResult;
  onDrillDown: (jobId: string) => void;
}) {
  const { icon, className } = deviceIcon(dr.status);
  const isDeviceActive = dr.status === 'running' || dr.status === 'retrying';
  return (
    <div className="flex items-center gap-2 py-1.5 text-sm border-b border-gray-50 last:border-0">
      <span className={`font-mono w-4 text-center flex-shrink-0 ${className}`}>{icon}</span>
      <span className="flex-1 text-text min-w-0 truncate">{dr.device}</span>
      <span className="flex-shrink-0">
        <StatusBadge status={dr.status} />
      </span>
      {isDeviceActive ? (
        <span className="text-xs text-muted/50 flex-shrink-0">running</span>
      ) : dr.duration_ms != null ? (
        <span className="text-xs text-muted/70 flex-shrink-0">{formatMs(dr.duration_ms)}</span>
      ) : null}
      {dr.retry_count > 0 && (
        <span className="text-xs text-amber-600 flex-shrink-0">↺{dr.retry_count}</span>
      )}
      {dr.rollback_performed && (
        <span className="text-xs text-orange-500 flex-shrink-0">↩</span>
      )}
      {dr.job_id && (
        <button
          onClick={() => onDrillDown(dr.job_id!)}
          className="flex-shrink-0 text-xs text-info hover:underline"
        >
          Details
        </button>
      )}
    </div>
  );
}

// ── Group job view ────────────────────────────────────────────────────────────

function GroupJobView({
  groupJob,
  onDrillDown,
}: {
  groupJob: GroupJob;
  onDrillDown: (jobId: string, device: string) => void;
}) {
  const { execution_summary: s, device_results, parameters, parameters_summary } = groupJob;
  const isGroupActive = groupJob.status === 'pending' || groupJob.status === 'running';
  // Fallback only -- covers a resource type that doesn't have a backend
  // `resumen_intento()` yet (or older data from before this field existed).
  const fallbackParams = parameters_summary ? null : meaningfulParameters(parameters);

  return (
    <div>
      <SectionHeader>Overview</SectionHeader>
      <Field label="Status">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusBadge status={groupJob.status} />
          {s.total_devices > 0 && (
            <span className="text-xs text-muted">
              {s.completed}/{s.total_devices} completed
              {s.failed > 0 && `, ${s.failed} failed`}
            </span>
          )}
          {isGroupActive && <ElapsedTimer startedAt={groupJob.started_at} />}
          {!isGroupActive && s.duration_ms != null && (
            <span className="text-xs text-muted/70">{formatMs(s.duration_ms)}</span>
          )}
        </div>
      </Field>
      <Field label="Operation">{groupJob.operation}</Field>
      <Field label="Group ID" mono>{groupJob.group_job_id}</Field>

      <SectionHeader>Timing</SectionHeader>
      <Field label="Created">{formatDateTime(groupJob.created_at)}</Field>
      <Field label="Started">{formatDateTime(groupJob.started_at)}</Field>
      <Field label="Finished">{formatDateTime(groupJob.finished_at)}</Field>
      {s.duration_ms != null && (
        <Field label="Duration">{formatMs(s.duration_ms)}</Field>
      )}

      {parameters_summary && (
        <>
          <SectionHeader>Change</SectionHeader>
          <p className="text-sm text-text">{parameters_summary}</p>
        </>
      )}
      {!parameters_summary && fallbackParams && (
        <>
          <SectionHeader>Change</SectionHeader>
          <JsonBlock data={fallbackParams} />
        </>
      )}

      <SectionHeader>Devices ({device_results.length})</SectionHeader>
      {device_results.length === 0 ? (
        <p className="text-xs text-muted/70">No device results yet.</p>
      ) : (
        <div>
          {device_results.map((dr) => (
            <DeviceRow
              key={dr.device}
              dr={dr}
              onDrillDown={(jobId) => onDrillDown(jobId, dr.device)}
            />
          ))}
        </div>
      )}
      {s.rollback_count > 0 && (
        <p className="text-xs text-warning mt-2">
          ↩ {s.rollback_count} device{s.rollback_count > 1 ? 's' : ''} rolled back
        </p>
      )}
    </div>
  );
}

// ── Loading / error states ────────────────────────────────────────────────────

function Spinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <div className="w-6 h-6 rounded-full border-2 border-panel-border border-t-blue-500 animate-spin" />
    </div>
  );
}

// ── Modal shell ───────────────────────────────────────────────────────────────

interface JobDetailModalProps {
  jobId?: string;
  groupJobId?: string;
  onClose: () => void;
}

export function JobDetailModal({ jobId, groupJobId, onClose }: JobDetailModalProps) {
  const [job, setJob] = useState<Job | null>(null);
  const [groupJob, setGroupJob] = useState<GroupJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // drill-down state: from group → single device job
  const [drillJobId, setDrillJobId] = useState<string | null>(null);
  const [drillDevice, setDrillDevice] = useState<string | null>(null);
  const [drillJob, setDrillJob] = useState<Job | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);

  // Fetch + keep polling while the job/group is still active, so a modal
  // left open while its job finishes reflects that -- rather than freezing
  // on whatever snapshot was fetched at open time (the bug: the job could
  // finish seconds after open and the modal would sit on "Running..."
  // forever, even though the toast behind it correctly went green).
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    setLoading(true);
    setFetchError(null);
    setJob(null);
    setGroupJob(null);
    setDrillJobId(null);
    setDrillJob(null);

    if (!jobId && !groupJobId) return;

    let cancelled = false;
    let timerId: ReturnType<typeof setInterval> | undefined;

    const fetchOnce = async () => {
      try {
        if (jobId) {
          const j = await getJob(jobId);
          if (cancelled) return;
          setJob(j);
          if (!(ACTIVE_JOB_STATUSES as string[]).includes(j.status) && timerId !== undefined) {
            clearInterval(timerId);
            timerId = undefined;
          }
        } else if (groupJobId) {
          const gj = await getGroupJob(groupJobId);
          if (cancelled) return;
          setGroupJob(gj);
          const isActive = gj.status === 'pending' || gj.status === 'running';
          if (!isActive && timerId !== undefined) {
            clearInterval(timerId);
            timerId = undefined;
          }
        }
      } catch {
        if (!cancelled) {
          setFetchError(jobId ? 'Failed to load job details.' : 'Failed to load group job details.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void fetchOnce();
    timerId = setInterval(fetchOnce, JOB_DETAIL_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timerId !== undefined) clearInterval(timerId);
    };
  }, [jobId, groupJobId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Same live-polling treatment for a drilled-down device job.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!drillJobId) { setDrillJob(null); return; }
    setDrillLoading(true);

    let cancelled = false;
    let timerId: ReturnType<typeof setInterval> | undefined;

    const fetchOnce = async () => {
      try {
        const j = await getJob(drillJobId);
        if (cancelled) return;
        setDrillJob(j);
        if (!(ACTIVE_JOB_STATUSES as string[]).includes(j.status) && timerId !== undefined) {
          clearInterval(timerId);
          timerId = undefined;
        }
      } catch {
        if (!cancelled) setDrillJob(null);
      } finally {
        if (!cancelled) setDrillLoading(false);
      }
    };

    void fetchOnce();
    timerId = setInterval(fetchOnce, JOB_DETAIL_POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timerId !== undefined) clearInterval(timerId);
    };
  }, [drillJobId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // ESC to close (or back from drill-down)
  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === 'Escape') {
      if (drillJobId) {
        setDrillJobId(null);
        setDrillDevice(null);
      } else {
        onClose();
      }
    }
  }, [drillJobId, onClose]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  // derive title
  let title = 'Job Details';
  if (groupJob) {
    const params = groupJob.parameters as Record<string, unknown> | null;
    const vlanId = params?.vlan_id;
    const op = groupJob.operation?.replace('_', ' ') ?? 'Operation';
    title = vlanId != null ? `${op} ${vlanId}` : op;
  } else if (job) {
    const op = job.operation?.replace('_', ' ');
    title = [op, job.device].filter(Boolean).join(' — ') || 'Job Details';
  }

  const handleDrillDown = (jobId: string, device: string) => {
    setDrillJobId(jobId);
    setDrillDevice(device);
  };

  const handleBack = () => {
    setDrillJobId(null);
    setDrillDevice(null);
    setDrillJob(null);
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center p-4 pt-16">
      {/* backdrop */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={drillJobId ? handleBack : onClose}
        aria-hidden
      />

      {/* panel */}
      <div className="relative bg-panel rounded-lg shadow-xl w-full max-w-lg flex flex-col max-h-[80vh]">
        {/* header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-panel-border flex-shrink-0">
          <h2 className="font-semibold text-text text-sm truncate pr-4">
            {drillDevice ? (
              <span>
                <span className="text-muted/70">{title} → </span>
                {drillDevice}
              </span>
            ) : title}
          </h2>
          <button
            onClick={onClose}
            className="text-muted/70 hover:text-muted text-xl leading-none flex-shrink-0"
            aria-label="Close"
          >
            &times;
          </button>
        </div>

        {/* body */}
        <div className="flex-1 overflow-y-auto px-4 py-3">
          {loading && <Spinner />}

          {fetchError && (
            <p className="text-sm text-red-600">{fetchError}</p>
          )}

          {/* single job (opened directly) */}
          {!loading && !fetchError && job && (
            <SingleJobView job={job} />
          )}

          {/* group job */}
          {!loading && !fetchError && groupJob && !drillJobId && (
            <GroupJobView groupJob={groupJob} onDrillDown={handleDrillDown} />
          )}

          {/* drill-down from group → device job */}
          {groupJob && drillJobId && (
            drillLoading
              ? <Spinner />
              : drillJob
                ? <SingleJobView
                    job={drillJob}
                    backLabel={title}
                    onBack={handleBack}
                  />
                : (
                  <div>
                    <button onClick={handleBack} className="text-xs text-info hover:underline mb-3">
                      ← {title}
                    </button>
                    <p className="text-sm text-red-600">Could not load device job details.</p>
                  </div>
                )
          )}
        </div>
      </div>
    </div>
  );
}
