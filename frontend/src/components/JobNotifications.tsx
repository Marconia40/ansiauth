'use client';

import { useState } from 'react';
import { useJobNotifications, type JobNotification, type GroupJobNotification } from '@/context/JobNotificationContext';
import { JobDetailModal } from '@/components/JobDetailModal';

type Selected =
  | { kind: 'job'; jobId: string }
  | { kind: 'group'; groupJobId: string }
  | null;

// ── Status helpers ────────────────────────────────────────────────────────────

// ── Notification cards ────────────────────────────────────────────────────────

// Always a small solid-color toast (same look the old per-tab `toast` divs
// had -- `bg-success text-white` / `bg-danger text-white`), for the whole
// lifetime of the card, not just once it reaches a terminal state -- the
// live status lives in the JobDetailModal this opens, the toast itself is
// just "something is happening / happened, click for detail". Genuine
// failure is still called out in red so it isn't missed.
function solidGroupToastClass(status: GroupJobNotification['status']): string {
  if (status === 'failed') return 'bg-danger text-white';
  if (status === 'partial_success' || status === 'partial_failure') return 'bg-orange-600 text-white';
  return 'bg-success text-white';
}

function GroupJobCard({
  gj,
  onOpen,
  onDismiss,
}: {
  gj: GroupJobNotification;
  onOpen: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === 'Enter' && onOpen()}
      className={`${solidGroupToastClass(gj.status)} rounded-md shadow-lg px-3 py-2 text-sm cursor-pointer hover:brightness-110 transition`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate">{gj.label}</span>
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="text-white/70 hover:text-white text-lg leading-none flex-shrink-0"
          aria-label="Dismiss"
        >
          &times;
        </button>
      </div>
    </div>
  );
}

// Same always-solid treatment as GroupJobCard. A no-op completion stays
// neutral (gray) rather than green -- "nothing changed" isn't quite the
// same signal as "applied" -- but pending/running still reads as green
// rather than flashing amber first, matching the group card.
function solidJobToastClass(job: JobNotification): string {
  if (job.status === 'failed') return 'bg-danger text-white';
  if (job.status === 'completed' && job.operationResult === 'noop') return 'bg-gray-600 text-white';
  return 'bg-success text-white';
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
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => e.key === 'Enter' && onOpen()}
      className={`${solidJobToastClass(job)} rounded-md shadow-lg px-3 py-2 text-sm cursor-pointer hover:brightness-110 transition`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate">
          {job.operation}{job.device ? ` — ${job.device}` : ''}
        </span>
        <button
          onClick={(e) => { e.stopPropagation(); onDismiss(); }}
          className="text-white/70 hover:text-white text-lg leading-none flex-shrink-0"
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
