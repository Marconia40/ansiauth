'use client';

import { useState, useEffect, useCallback } from 'react';
import { getJob, getGroupJob } from '@/services/api';
import type { Job, GroupJob, GroupJobDeviceResult } from '@/types/job';

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

// ── Building blocks ───────────────────────────────────────────────────────────

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2 mt-4 first:mt-0">
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
      <span className="w-32 flex-shrink-0 text-gray-500">{label}</span>
      <span className={`flex-1 break-all ${mono ? 'font-mono text-xs' : 'text-gray-900'}`}>
        {children ?? <span className="text-gray-300">—</span>}
      </span>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const classes: Record<string, string> = {
    completed: 'bg-green-100 text-green-700',
    failed: 'bg-red-100 text-red-700',
    cancelled: 'bg-red-100 text-red-700',
    running: 'bg-amber-100 text-amber-700',
    retrying: 'bg-amber-100 text-amber-700',
    pending: 'bg-gray-100 text-gray-600',
    partial_success: 'bg-orange-100 text-orange-700',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${classes[status] ?? 'bg-gray-100 text-gray-600'}`}>
      {status.replace('_', ' ')}
    </span>
  );
}

function JsonBlock({ data }: { data: unknown }) {
  if (data == null) return <span className="text-gray-300 text-xs">null</span>;
  const text = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  return (
    <pre className="mt-1 text-xs bg-gray-50 border border-gray-100 rounded p-2 overflow-auto max-h-36 text-gray-700 whitespace-pre-wrap break-all">
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
      {success === null && <span className="text-gray-400">unverified</span>}
    </Field>
  );
}

// ── Single job view ───────────────────────────────────────────────────────────

function SingleJobView({ job, backLabel, onBack }: { job: Job; backLabel?: string; onBack?: () => void }) {
  const durationMs = job.execution_summary?.duration_ms ?? deriveDurationMs(job.started_at, job.finished_at);
  const attempts = job.execution_summary?.attempts;
  const hasPreState = job.pre_state != null;
  const hasResult = job.result != null;
  const hasError = !!(job.error || job.last_error);

  return (
    <div>
      {onBack && (
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-xs text-blue-600 hover:underline mb-3"
        >
          ← {backLabel ?? 'Back'}
        </button>
      )}

      <SectionHeader>Overview</SectionHeader>
      <Field label="Status"><StatusBadge status={job.status} /></Field>
      <Field label="Device">{job.device}</Field>
      <Field label="Playbook">{job.playbook}</Field>
      <Field label="Job ID" mono>{job.job_id}</Field>
      {job.group_job_id && (
        <Field label="Group Job" mono>{job.group_job_id}</Field>
      )}

      <SectionHeader>Execution</SectionHeader>
      <Field label="Duration">{formatMs(durationMs)}</Field>
      {attempts != null && (
        <Field label="Attempts">{attempts} / {job.max_retries + 1}</Field>
      )}
      {attempts == null && (
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

      {hasError && (
        <>
          <SectionHeader>Error</SectionHeader>
          <div className="text-sm text-red-700 bg-red-50 border border-red-100 rounded p-2 break-all">
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
  if (status === 'running' || status === 'retrying') return { icon: '↻', className: 'text-amber-600' };
  return { icon: '·', className: 'text-gray-400' };
}

function DeviceRow({
  dr,
  onDrillDown,
}: {
  dr: GroupJobDeviceResult;
  onDrillDown: (jobId: string) => void;
}) {
  const { icon, className } = deviceIcon(dr.status);
  return (
    <div className="flex items-center gap-2 py-1.5 text-sm border-b border-gray-50 last:border-0">
      <span className={`font-mono w-4 text-center flex-shrink-0 ${className}`}>{icon}</span>
      <span className="flex-1 text-gray-900 min-w-0 truncate">{dr.device}</span>
      <span className="flex-shrink-0">
        <StatusBadge status={dr.status} />
      </span>
      {dr.duration_ms != null && (
        <span className="text-xs text-gray-400 flex-shrink-0">{formatMs(dr.duration_ms)}</span>
      )}
      {dr.retry_count > 0 && (
        <span className="text-xs text-amber-600 flex-shrink-0">↺{dr.retry_count}</span>
      )}
      {dr.rollback_performed && (
        <span className="text-xs text-orange-500 flex-shrink-0">↩</span>
      )}
      {dr.job_id && (
        <button
          onClick={() => onDrillDown(dr.job_id!)}
          className="flex-shrink-0 text-xs text-blue-600 hover:underline"
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
  const { execution_summary: s, device_results, parameters } = groupJob;

  return (
    <div>
      <SectionHeader>Overview</SectionHeader>
      <Field label="Status">
        <div className="flex items-center gap-2">
          <StatusBadge status={groupJob.status} />
          {s.total_devices > 0 && (
            <span className="text-xs text-gray-500">
              {s.completed}/{s.total_devices} completed
              {s.failed > 0 && `, ${s.failed} failed`}
            </span>
          )}
        </div>
      </Field>
      <Field label="Operation">{groupJob.operation}</Field>
      <Field label="Playbook">{groupJob.playbook}</Field>
      {parameters && (
        <Field label="Parameters" mono>
          {Object.entries(parameters)
            .map(([k, v]) => `${k}: ${v}`)
            .join(', ')}
        </Field>
      )}
      <Field label="Group ID" mono>{groupJob.group_job_id}</Field>

      <SectionHeader>Timing</SectionHeader>
      <Field label="Created">{formatDateTime(groupJob.created_at)}</Field>
      <Field label="Started">{formatDateTime(groupJob.started_at)}</Field>
      <Field label="Finished">{formatDateTime(groupJob.finished_at)}</Field>
      {s.duration_ms != null && (
        <Field label="Duration">{formatMs(s.duration_ms)}</Field>
      )}

      <SectionHeader>Devices ({device_results.length})</SectionHeader>
      {device_results.length === 0 ? (
        <p className="text-xs text-gray-400">No device results yet.</p>
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
        <p className="text-xs text-orange-600 mt-2">
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
      <div className="w-6 h-6 rounded-full border-2 border-gray-200 border-t-blue-500 animate-spin" />
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

  // initial fetch
  useEffect(() => {
    setLoading(true);
    setFetchError(null);
    setJob(null);
    setGroupJob(null);
    setDrillJobId(null);
    setDrillJob(null);

    if (jobId) {
      getJob(jobId)
        .then(setJob)
        .catch(() => setFetchError('Failed to load job details.'))
        .finally(() => setLoading(false));
    } else if (groupJobId) {
      getGroupJob(groupJobId)
        .then(setGroupJob)
        .catch(() => setFetchError('Failed to load group job details.'))
        .finally(() => setLoading(false));
    }
  }, [jobId, groupJobId]);

  // drill-down fetch
  useEffect(() => {
    if (!drillJobId) { setDrillJob(null); return; }
    setDrillLoading(true);
    getJob(drillJobId)
      .then(setDrillJob)
      .catch(() => setDrillJob(null))
      .finally(() => setDrillLoading(false));
  }, [drillJobId]);

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
    title = [job.playbook, job.device].filter(Boolean).join(' — ');
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
      <div className="relative bg-white rounded-lg shadow-xl w-full max-w-lg flex flex-col max-h-[80vh]">
        {/* header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 flex-shrink-0">
          <h2 className="font-semibold text-gray-900 text-sm truncate pr-4">
            {drillDevice ? (
              <span>
                <span className="text-gray-400">{title} → </span>
                {drillDevice}
              </span>
            ) : title}
          </h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-xl leading-none flex-shrink-0"
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
                    <button onClick={handleBack} className="text-xs text-blue-600 hover:underline mb-3">
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
