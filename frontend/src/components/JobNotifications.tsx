'use client';

import { useJobNotifications } from '@/context/JobNotificationContext';

type JobNotification = {
  jobId: string;
  operation: string;
  device?: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  message: string;
  operationResult?: string;
  reason?: string;
};

function getDisplayState(job: JobNotification): { label: string; className: string } {
  if (job.status === 'completed' && job.operationResult === 'noop') {
    return { label: 'No changes', className: 'bg-gray-100 text-gray-700' };
  }
  if (job.status === 'completed') {
    return { label: 'Completed', className: 'bg-green-100 text-green-700' };
  }
  if (job.status === 'failed') {
    return { label: 'Failed', className: 'bg-red-100 text-red-700' };
  }
  if (job.status === 'running') {
    return { label: 'Running', className: 'bg-amber-100 text-amber-700' };
  }
  return { label: 'Pending', className: 'bg-amber-100 text-amber-700' };
}

function getNoopMessage(reason?: string): string {
  if (reason === 'vlan_already_exists_no_op') return 'VLAN already exists — no changes applied';
  if (reason === 'vlan_name_unchanged_no_op') return 'VLAN name already matches current configuration';
  return 'No changes were required';
}

export function JobNotifications() {
  const { jobs, dismissJob } = useJobNotifications();

  if (jobs.length === 0) return null;

  const visible = jobs.slice(0, 5);

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-72 max-h-96 overflow-y-auto">
      {visible.map((job) => {
        const display = getDisplayState(job);
        const isNoop = job.status === 'completed' && job.operationResult === 'noop';
        return (
          <div
            key={job.jobId}
            className="bg-white border border-gray-200 rounded-md shadow-sm p-3 text-sm"
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
                onClick={() => dismissJob(job.jobId)}
                className="text-gray-400 hover:text-gray-600 text-lg leading-none mt-0.5 flex-shrink-0"
                aria-label="Dismiss"
              >
                &times;
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
