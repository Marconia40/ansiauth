'use client';

import { useState, useEffect, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getDevice, getJob, getGroupJob, retryJobRollback } from '@/services/api';
import { StatusBadge } from '@/components/StatusBadge';
import { ElapsedTimer } from '@/components/ElapsedTimer';
import { useCanPerform } from '@/hooks/useAuthz';
import { ACTIVE_JOB_STATUSES } from '@/types/job';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Device } from '@/types/device';
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

// Strips leftover terminal-control artifacts from a raw SSH transcript --
// e.g. a cursor-left sequence from the device's own line-wrap redraw shows
// up as literal "[1D" text once the actual ESC byte is gone (confirmed live
// against f3r9s2: "max-ra[1Dte percent 10" is really "max-ra" + cursor-left
// 1 + "te percent 10", i.e. just "max-rate percent 10" redrawn). Matches
// both the real \x1b-prefixed CSI form (if it ever survives) and the
// bare leftover form -- deliberately narrow (cursor movement only, digit
// required) so it doesn't also eat a device prompt's own brackets, e.g.
// "[f3r9s2-GigabitEthernet0/0/23]" starts with "[f", which a looser
// version of this regex (any letter, 0+ digits) wrongly stripped down to
// "3r9s2-...".
const _ANSI_CSI_RE = /\x1b?\[\d+[ABCD]/g;

function cleanTranscript(raw: string): string {
  return raw.replace(_ANSI_CSI_RE, '');
}

// A device's real rejection reason ("% Invalid input...", Cisco; "Error:
// Unrecognized command...", Huawei) is 1-2 lines buried inside a full SSH
// session transcript (banners, prompts, every command echoed back) --
// confirmed as the actual complaint: "dificil decodificarlo... que error
// tiró??". Mirrors the same marker ssh_direct_service.py's `_ERROR_RE`
// already uses server-side to detect a rejection at all -- this just
// re-finds those same lines client-side to surface them, it doesn't
// invent a new definition of "error line".
const _DEVICE_ERROR_LINE_RE = /^\s*(Error:.*|%\s.*)$/;

interface DeviceErrorHit {
  command: string | null;
  error: string;
}

function extractDeviceErrors(raw: string): DeviceErrorHit[] {
  const lines = cleanTranscript(raw).split(/\r?\n/);
  const hits: DeviceErrorHit[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line || !_DEVICE_ERROR_LINE_RE.test(line)) continue;
    // Walk back past the "^" position-marker line (if present) and any
    // blank lines to the actual command that got rejected -- that's the
    // useful context, not just the error text alone.
    let command: string | null = null;
    for (let j = i - 1; j >= 0; j--) {
      const prev = lines[j].trim();
      if (!prev || prev === '^') continue;
      if (_DEVICE_ERROR_LINE_RE.test(prev)) break; // hit the previous error, stop
      command = prev;
      break;
    }
    hits.push({ command, error: line });
  }
  return hits;
}

// Shape of a successful driver result -- {rc, stdout, stderr, stdouts,
// success} for Puerto/SVI/GlobalConfig operations that go through a real
// SSH session (see aplicar_paso()/aplicar_lote() in the backend); `noop`/
// `accion` are added by the resource's own aplicar() before Orquestador
// ever sees it. Not every resource type produces this exact shape (VLAN's
// simpler operations may not), so DeviceResultDetails falls back to the
// raw JSON dump for anything that doesn't look like it.
interface DeviceResultShape {
  rc?: number;
  stdout?: string;
  success?: boolean;
  noop?: boolean;
  commands?: string[];
}

// Same idea as DeviceErrorDetails, mirrored for the success case: the raw
// stdout is either the full ansible-playbook console dump (Cisco's
// ios_config exposes no stdout at all, see ansible_service.py) or the
// device's own full session echo (Huawei's cli_command does return
// stdout, but it's VTY banners + every prompt/command echoed back) --
// equally unreadable for different reasons. `commands` (see
// vendors/base.py::_comandos_desde_extravars()) sidesteps both by
// surfacing exactly what was sent, known before either transport ever
// ran. The raw transcript stays one click away for anyone who wants to
// verify the literal bytes.
function DeviceResultDetails({ result }: { result: unknown }) {
  if (result == null || typeof result !== 'object') {
    return <JsonBlock data={result} />;
  }
  const r = result as DeviceResultShape;
  if (typeof r.stdout !== 'string') {
    return <JsonBlock data={result} />;
  }
  const cleaned = cleanTranscript(r.stdout);
  return (
    <div className="flex flex-col gap-1.5">
      <p className="text-sm text-green-600">
        {r.noop
          ? 'No changes needed — device already matched the requested state.'
          : '✓ Applied successfully.'}
      </p>
      {r.commands && r.commands.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-muted">Commands applied:</span>
          <pre className="text-xs font-mono text-text bg-panel-elev/60 border border-panel-border rounded p-2 whitespace-pre-wrap break-all">
            {r.commands.join('\n')}
          </pre>
        </div>
      )}
      {cleaned.trim() !== '' && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted hover:text-text select-none">
            Show full session transcript
          </summary>
          <pre className="mt-1.5 text-xs text-text bg-panel-elev/60 border border-panel-border rounded p-2 whitespace-pre-wrap break-all max-h-64 overflow-auto">
            {cleaned}
          </pre>
        </details>
      )}
    </div>
  );
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

// Small pill next to the Error label -- gives the user a one-glance
// answer to "was this a permanent bug or a transient hiccup?" without
// having to read the raw stderr. Colors match the semantic weight
// (permanent = red, transient = amber, unknown = neutral).
function ErrorTypeBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const colors =
    type === 'permanent' ? 'bg-danger/15 text-danger'
    : type === 'transient' ? 'bg-warning/15 text-warning'
    : 'bg-muted/15 text-muted';
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide ${colors}`}>
      {type}
    </span>
  );
}

// Rendered when rollback ran and did NOT succeed. Two-part display:
// friendly summary on top (safe to show in list rows and tooltips),
// raw device output below for debug when the user needs the exact
// bytes. Also hosts the "Retry rollback" action so the user can trigger
// recovery from the same modal without navigating away.
function RollbackFailureSection({
  job,
  onRetry,
  retryState,
}: {
  job: Job;
  onRetry: () => void;
  retryState: { pending: boolean; error: string | null; newJobId: string | null };
}) {
  // Only supported operations can be retried -- the endpoint enforces
  // this too, but hiding the button avoids the user clicking and
  // getting a 400 for no reason.
  const canRetryOperation = job.operation === 'puerto' || job.operation === 'svi';
  // Per-scope role gate: the backend enforces ``min_role="operator"`` on
  // POST /jobs/{id}/retry-rollback via ``authorize_device()`` against
  // the job's original device. Since Job doesn't carry site_id/
  // device_group_id (would need N+1 batching on /jobs list), we resolve
  // them on-demand by fetching the device row when the modal renders a
  // job that has one. React Query caches by device name so reopening
  // the same job doesn't refetch.
  const { data: jobDevice } = useQuery<Device>({
    queryKey: ['device', job.device],
    queryFn: () => getDevice(job.device!),
    enabled: !!job.device && canRetryOperation,
  });
  const scopeForGate = jobDevice
    ? { siteId: jobDevice.site_id, deviceGroupId: jobDevice.device_group_id }
    : null;
  const { allowed: hasWriteRole } = useCanPerform('retry_rollback', scopeForGate);
  const canRetry = canRetryOperation && hasWriteRole;

  // Existing retry-rollback jobs -- the source of truth for "has this
  // already been retried?" that persists across modal reopens (unlike
  // the local retryState, which is lost when the modal closes). The
  // backend populates the list only when this job has
  // rollback_success=false (see api/jobs.py:_format_job).
  const existingRetries = job.retry_rollback_jobs ?? [];
  const inProgress = existingRetries.find(
    (r) => r.status === 'pending' || r.status === 'running' || r.status === 'retrying',
  );
  const latestCompleted = existingRetries.find((r) => r.status === 'completed');

  return (
    <>
      <SectionHeader>Rollback failure</SectionHeader>
      {job.rollback_error && (
        <div className="text-sm text-danger bg-danger/10 border border-danger/40 rounded p-2 break-all mb-2">
          {job.rollback_error}
        </div>
      )}
      {!job.rollback_error && (
        <p className="text-xs text-muted/70 mb-2">
          Rollback failed but no explanatory output was captured (legacy job
          created before rollback-error persistence was added).
        </p>
      )}
      {canRetry ? (
        <div className="flex items-center gap-2 flex-wrap">
          <button
            onClick={onRetry}
            // Button is disabled in 4 cases:
            //  1. an API call is in flight (pending)
            //  2. the current session already queued one (newJobId)
            //  3. there's a pending/running retry from any session (inProgress)
            //  4. a completed retry already succeeded (latestCompleted)
            // Cases 3-4 come from the backend-supplied
            // retry_rollback_jobs list and are the guard against
            // "click retry, close modal, reopen, click retry again".
            disabled={
              retryState.pending
              || retryState.newJobId !== null
              || inProgress !== undefined
              || latestCompleted !== undefined
            }
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded bg-warning/20 text-warning hover:bg-warning/30 disabled:opacity-50 disabled:cursor-not-allowed transition"
          >
            {retryState.pending ? '↻ Queuing...' : '↩ Retry rollback'}
          </button>
          {/* Backend-persistent state takes precedence over local -- if
              we already know a retry ran, don't show a stale "queued"
              message from an earlier session. */}
          {inProgress ? (
            <span className="text-xs text-amber-600">
              ↻ Retry in progress (job {inProgress.job_id.slice(0, 8)}...)
            </span>
          ) : latestCompleted ? (
            <span className="text-xs text-green-600">
              ✓ Already recovered by job {latestCompleted.job_id.slice(0, 8)}...
              {latestCompleted.finished_at && (
                <> at {new Date(latestCompleted.finished_at).toLocaleString(undefined, {
                  hour: '2-digit', minute: '2-digit',
                })}</>
              )}
            </span>
          ) : retryState.newJobId ? (
            <span className="text-xs text-green-600">
              ✓ Queued as new job — track it in notifications
            </span>
          ) : null}
          {retryState.error && (
            <span className="text-xs text-red-600">{retryState.error}</span>
          )}
        </div>
      ) : !canRetryOperation ? (
        <p className="text-xs text-muted/70">
          Retry-rollback is only available for port and SVI jobs.
        </p>
      ) : (
        // canRetryOperation but !hasWriteRole -- observer role. Give a
        // clear reason instead of just hiding the section (they'd still
        // see "Rollback failure" and the raw error, so a note is more
        // useful than mystery).
        <p className="text-xs text-muted/70">
          Retry-rollback requires operator role or higher.
        </p>
      )}
    </>
  );
}

// ── Single job view ───────────────────────────────────────────────────────────

function SingleJobView({ job, backLabel, onBack }: { job: Job; backLabel?: string; onBack?: () => void }) {
  const { trackJob } = useJobNotifications();
  const durationMs = job.execution_summary?.duration_ms ?? deriveDurationMs(job.started_at, job.finished_at);
  const attempts = job.execution_summary?.attempts;
  const isActive = (ACTIVE_JOB_STATUSES as string[]).includes(job.status);
  const hasPreState = job.pre_state != null;
  const hasResult = job.result != null;
  const hasError = !!(job.error || job.last_error);
  // error_summary is the short English one-liner produced by the
  // backend classifier. Preferred for display; the raw `error` still
  // shows below in the Full Error section for debug.
  const errorSummary = job.error_summary;
  const showRollbackFailure = job.rollback_performed && job.rollback_success === false;

  // Retry-rollback state -- posts to /jobs/{id}/retry-rollback and
  // hands the returned job id to the notification tracker so the user
  // sees the recovery job's progress in the toast area.
  const [retryState, setRetryState] = useState<{ pending: boolean; error: string | null; newJobId: string | null }>({
    pending: false, error: null, newJobId: null,
  });
  const handleRetryRollback = useCallback(async () => {
    setRetryState({ pending: true, error: null, newJobId: null });
    try {
      const res = await retryJobRollback(job.job_id);
      // Hook the new job into notifications so the user gets live
      // status updates on the recovery attempt without having to
      // manually navigate to it.
      trackJob(res.job_id, 'retry_rollback', job.device ?? undefined);
      setRetryState({ pending: false, error: null, newJobId: res.job_id });
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to queue retry';
      setRetryState({ pending: false, error: message, newJobId: null });
    }
  }, [job.job_id, job.device, trackJob]);

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
      {hasError && (
        <Field label="Error">
          <div className="flex items-start gap-2 flex-wrap">
            <ErrorTypeBadge type={job.error_type} />
            {/* Prefer the friendly one-liner. Fall back to the raw first
                line if the backend didn't classify (legacy jobs / brand
                new device errors that no pattern has caught yet). */}
            <span className="text-red-600 text-xs flex-1 min-w-0">
              {errorSummary ?? job.error ?? job.last_error}
            </span>
          </div>
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

      {showRollbackFailure && (
        <RollbackFailureSection
          job={job}
          onRetry={handleRetryRollback}
          retryState={retryState}
        />
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
          <DeviceResultDetails result={job.result} />
        </>
      )}

      {hasError && (
        <>
          <SectionHeader>Error details</SectionHeader>
          <DeviceErrorDetails raw={job.error ?? job.last_error ?? ''} />
        </>
      )}
    </div>
  );
}

// Renders the lines a device actually rejected (see extractDeviceErrors())
// front and center; the full raw transcript stays available but collapsed
// by default -- it's still occasionally useful (e.g. to see exactly which
// port/step in a batch got there), just not the first thing to read.
function DeviceErrorDetails({ raw }: { raw: string }) {
  const hits = extractDeviceErrors(raw);
  const cleaned = cleanTranscript(raw);
  if (hits.length === 0) {
    return (
      <pre className="text-sm text-danger bg-danger/10 border border-danger/40 rounded p-2 whitespace-pre-wrap break-all">
        {cleaned}
      </pre>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-1.5">
        {hits.map((hit, i) => (
          <div key={i} className="text-sm bg-danger/10 border border-danger/40 rounded p-2">
            {hit.command && (
              <div className="font-mono text-xs text-muted mb-0.5 break-all">{hit.command}</div>
            )}
            <div className="text-danger break-all">{hit.error}</div>
          </div>
        ))}
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted hover:text-text select-none">
          Show full session transcript
        </summary>
        <pre className="mt-1.5 text-xs text-danger bg-danger/10 border border-danger/40 rounded p-2 whitespace-pre-wrap break-all max-h-64 overflow-auto">
          {cleaned}
        </pre>
      </details>
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
  // Same "hover for details, click for full modal" pattern as the /jobs
  // listing -- the summary is safe/short enough to preview inline; raw
  // error stays available as a tooltip without needing to drill in.
  const summary = dr.status === 'failed' ? (dr.error_summary ?? dr.error) : null;
  return (
    <div className="border-b border-gray-50 last:border-0">
      <div className="flex items-center gap-2 py-1.5 text-sm">
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
          <span
            className={`text-xs flex-shrink-0 ${dr.rollback_success === false ? 'text-red-600' : 'text-orange-500'}`}
            title={dr.rollback_success === false ? (dr.rollback_error ?? 'Rollback failed') : 'Rollback executed'}
          >
            ↩
          </span>
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
      {/* Short reason inline, no drill-down needed for the common case --
          before this, a device's error was invisible unless you clicked
          into its own job. */}
      {summary && (
        <p
          className="pl-6 pb-1.5 text-xs text-danger truncate"
          title={dr.error ?? undefined}
        >
          {summary}
        </p>
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
