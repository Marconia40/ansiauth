'use client';

import { useState } from 'react';
import { useJobNotifications, type JobNotification, type GroupJobNotification } from '@/context/JobNotificationContext';
import { JobDetailModal } from '@/components/JobDetailModal';

type Selected =
  | { kind: 'job'; jobId: string }
  | { kind: 'group'; groupJobId: string }
  | null;

// ── Status helpers ────────────────────────────────────────────────────────────

function getJobDisplayState(job: JobNotification): { label: string; className: string } {
  if (job.status === 'completed' && job.operationResult === 'noop') {
    return { label: 'No changes', className: 'bg-gray-100 text-gray-700' };
  }
  if (job.status === 'completed') return { label: 'Completed', className: 'bg-green-100 text-green-700' };
  if (job.status === 'failed') return { label: 'Failed', className: 'bg-red-100 text-red-700' };
  if (job.status === 'running') return { label: 'Running', className: 'bg-amber-100 text-amber-700' };
  return { label: 'Pending', className: 'bg-amber-100 text-amber-700' };
}

function getNoopMessage(reason?: string): string {
  if (reason === 'vlan_already_exists_no_op') return 'VLAN already exists — no changes applied';
  if (reason === 'vlan_name_unchanged_no_op') return 'VLAN name already matches current configuration';
  return 'No changes were required';
}

function getGroupSummary(gj: GroupJobNotification): { text: string; badgeLabel: string; badgeClass: string } {
  const { status, total, completed, failed } = gj;
  if (status === 'pending') {
    return { text: `(0/${total || '…'})`, badgeLabel: 'Pending', badgeClass: 'bg-amber-100 text-amber-700' };
  }
  if (status === 'running') {
    return { text: `(${completed}/${total} completed)`, badgeLabel: 'Running', badgeClass: 'bg-amber-100 text-amber-700' };
  }
  if (status === 'completed') {
    return { text: `— completed (${total}/${total})`, badgeLabel: 'Completed', badgeClass: 'bg-green-100 text-green-700' };
  }
  if (status === 'partial_success') {
    return {
      text: `— partial failure (${completed} success, ${failed} failed)`,
      badgeLabel: 'Partial',
      badgeClass: 'bg-orange-100 text-orange-700',
    };
  }
  return { text: '— failed', badgeLabel: 'Failed', badgeClass: 'bg-red-100 text-red-700' };
}

// ── Notification cards ────────────────────────────────────────────────────────

function GroupJobCard({
  gj,
  onOpen,
  onDismiss,
}: {
  gj: GroupJobNotification;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  const { text, badgeLabel, badgeClass } = getGroupSummary(gj);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === 'Enter' && onOpen()}
      className="bg-white border border-gray-200 rounded-md shadow-sm p-3 text-sm cursor-pointer hover:border-blue-300 hover:shadow-md transition-shadow"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-gray-900 truncate">
            {gj.label} {text}
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${badgeClass}`}>
              {badgeLabel}
            </span>
            <span className="text-xs text-gray-400">Click for details</span>
          </div>
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="text-gray-400 hover:text-gray-600 text-lg leading-none mt-0.5 flex-shrink-0"
          aria-label="Dismiss"
        >
          &times;
        </button>
      </div>
    </div>
  );
}

function JobCard({
  job,
  onOpen,
  onDismiss,
}: {
  job: JobNotification;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  const display = getJobDisplayState(job);
  const isNoop = job.status === 'completed' && job.operationResult === 'noop';

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === 'Enter' && onOpen()}
      className="bg-white border border-gray-200 rounded-md shadow-sm p-3 text-sm cursor-pointer hover:border-blue-300 hover:shadow-md transition-shadow"
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="font-medium text-gray-900 truncate">
            {job.operation}{job.device ? ` — ${job.device}` : ''}
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${display.className}`}>
              {display.label}
            </span>
            {job.status === 'failed' && (
              <span className="text-gray-500 text-xs truncate">{job.message}</span>
            )}
          </div>
          {isNoop && (
            <div className="mt-1 text-xs text-gray-500">{getNoopMessage(job.reason)}</div>
          )}
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="text-gray-400 hover:text-gray-600 text-lg leading-none mt-0.5 flex-shrink-0"
          aria-label="Dismiss"
        >
          &times;
        </button>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function JobNotifications() {
  const { jobs, groupJobs, dismissJob, dismissGroupJob } = useJobNotifications();
  const [selected, setSelected] = useState<Selected>(null);

  const hasNotifications = jobs.length > 0 || groupJobs.length > 0;
  if (!hasNotifications && !selected) return null;

  const visibleGroupJobs = groupJobs.slice(0, 3);
  const remainingSlots = Math.max(0, 5 - visibleGroupJobs.length);
  const visibleJobs = jobs.slice(0, remainingSlots);

  return (
    <>
      <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-72 max-h-[32rem] overflow-y-auto">
        {visibleGroupJobs.map((gj) => (
          <GroupJobCard
            key={gj.groupJobId}
            gj={gj}
            onOpen={() => setSelected({ kind: 'group', groupJobId: gj.groupJobId })}
            onDismiss={() => dismissGroupJob(gj.groupJobId)}
          />
        ))}

        {visibleJobs.map((job) => (
          <JobCard
            key={job.jobId}
            job={job}
            onOpen={() => setSelected({ kind: 'job', jobId: job.jobId })}
            onDismiss={() => dismissJob(job.jobId)}
          />
        ))}
      </div>

      {selected?.kind === 'job' && (
        <JobDetailModal jobId={selected.jobId} onClose={() => setSelected(null)} />
      )}
      {selected?.kind === 'group' && (
        <JobDetailModal groupJobId={selected.groupJobId} onClose={() => setSelected(null)} />
      )}
    </>
  );
}
