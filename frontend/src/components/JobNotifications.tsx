'use client';

import { useState } from 'react';
import { useJobNotifications, type JobNotification, type GroupJobNotification } from '@/context/JobNotificationContext';

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
    return {
      text: `(0/${total || '…'})`,
      badgeLabel: 'Pending',
      badgeClass: 'bg-amber-100 text-amber-700',
    };
  }
  if (status === 'running') {
    return {
      text: `(${completed}/${total} completed)`,
      badgeLabel: 'Running',
      badgeClass: 'bg-amber-100 text-amber-700',
    };
  }
  if (status === 'completed') {
    return {
      text: `— completed (${total}/${total})`,
      badgeLabel: 'Completed',
      badgeClass: 'bg-green-100 text-green-700',
    };
  }
  if (status === 'partial_success') {
    return {
      text: `— partial failure (${completed} success, ${failed} failed)`,
      badgeLabel: 'Partial',
      badgeClass: 'bg-orange-100 text-orange-700',
    };
  }
  return {
    text: '— failed',
    badgeLabel: 'Failed',
    badgeClass: 'bg-red-100 text-red-700',
  };
}

function deviceStatusIcon(status: string): { icon: string; className: string } {
  if (status === 'completed') return { icon: '✓', className: 'text-green-600' };
  if (status === 'failed' || status === 'cancelled') return { icon: '✗', className: 'text-red-600' };
  if (status === 'running' || status === 'retrying') return { icon: '↻', className: 'text-amber-600' };
  return { icon: '·', className: 'text-gray-400' };
}

export function JobNotifications() {
  const { jobs, groupJobs, dismissJob, dismissGroupJob } = useJobNotifications();
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const hasNotifications = jobs.length > 0 || groupJobs.length > 0;
  if (!hasNotifications) return null;

  function toggleExpand(groupJobId: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(groupJobId)) next.delete(groupJobId);
      else next.add(groupJobId);
      return next;
    });
  }

  const visibleGroupJobs = groupJobs.slice(0, 3);
  const remainingSlots = Math.max(0, 5 - visibleGroupJobs.length);
  const visibleJobs = jobs.slice(0, remainingSlots);

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 w-72 max-h-[32rem] overflow-y-auto">
      {visibleGroupJobs.map((gj) => {
        const { text, badgeLabel, badgeClass } = getGroupSummary(gj);
        const isExpanded = expandedGroups.has(gj.groupJobId);
        const hasDevices = gj.deviceResults.length > 0;

        return (
          <div
            key={gj.groupJobId}
            className="bg-white border border-gray-200 rounded-md shadow-sm p-3 text-sm"
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
                  {hasDevices && (
                    <button
                      onClick={() => toggleExpand(gj.groupJobId)}
                      className="text-xs text-blue-600 hover:underline"
                    >
                      {isExpanded ? 'Hide details' : 'Details'}
                    </button>
                  )}
                </div>
                {isExpanded && hasDevices && (
                  <div className="mt-2 space-y-1 border-t border-gray-100 pt-2">
                    {gj.deviceResults.map((dr) => {
                      const { icon, className } = deviceStatusIcon(dr.status);
                      return (
                        <div key={dr.device} className="flex items-start gap-1.5 text-xs">
                          <span className={`mt-0.5 font-mono ${className}`}>{icon}</span>
                          <div className="min-w-0">
                            <span className="text-gray-700">{dr.device}</span>
                            {dr.retry_count > 0 && (
                              <span className="ml-1 text-gray-400">(retry {dr.retry_count})</span>
                            )}
                            {dr.rollback_performed && (
                              <span className="ml-1 text-orange-500">↩ rolled back</span>
                            )}
                            {dr.error && (
                              <div className="text-red-500 truncate">{dr.error}</div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
              <button
                onClick={() => dismissGroupJob(gj.groupJobId)}
                className="text-gray-400 hover:text-gray-600 text-lg leading-none mt-0.5 flex-shrink-0"
                aria-label="Dismiss"
              >
                &times;
              </button>
            </div>
          </div>
        );
      })}

      {visibleJobs.map((job) => {
        const display = getJobDisplayState(job);
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
