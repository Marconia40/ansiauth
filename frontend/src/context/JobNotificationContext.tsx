'use client';

import { createContext, useContext, useState, useRef, useCallback, useEffect, type ReactNode } from 'react';
import { getJob, getGroupJob } from '@/services/api';
import type { GroupJobStatus, GroupJobDeviceResult } from '@/types/job';

export type JobNotification = {
  jobId: string;
  operation: string;
  device?: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  message: string;
  operationResult?: string;
  reason?: string;
};

export type GroupJobNotification = {
  groupJobId: string;
  label: string;
  status: GroupJobStatus;
  total: number;
  completed: number;
  failed: number;
  deviceResults: GroupJobDeviceResult[];
};

type JobNotificationContextValue = {
  jobs: JobNotification[];
  groupJobs: GroupJobNotification[];
  trackJob: (jobId: string, operation: string, device?: string) => void;
  trackGroupJob: (groupJobId: string, label: string) => void;
  dismissJob: (jobId: string) => void;
  dismissGroupJob: (groupJobId: string) => void;
};

const JobNotificationContext = createContext<JobNotificationContextValue | null>(null);

export function JobNotificationProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<JobNotification[]>([]);
  const [groupJobs, setGroupJobs] = useState<GroupJobNotification[]>([]);

  const intervalsRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const dismissTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  const groupIntervalsRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const groupDismissTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  useEffect(() => {
    return () => {
      intervalsRef.current.forEach((iv) => clearInterval(iv));
      dismissTimersRef.current.forEach((t) => clearTimeout(t));
      groupIntervalsRef.current.forEach((iv) => clearInterval(iv));
      groupDismissTimersRef.current.forEach((t) => clearTimeout(t));
    };
  }, []);

  const dismissJob = useCallback((jobId: string) => {
    const iv = intervalsRef.current.get(jobId);
    if (iv !== undefined) { clearInterval(iv); intervalsRef.current.delete(jobId); }
    const t = dismissTimersRef.current.get(jobId);
    if (t !== undefined) { clearTimeout(t); dismissTimersRef.current.delete(jobId); }
    setJobs((prev) => prev.filter((n) => n.jobId !== jobId));
  }, []);

  const dismissGroupJob = useCallback((groupJobId: string) => {
    const iv = groupIntervalsRef.current.get(groupJobId);
    if (iv !== undefined) { clearInterval(iv); groupIntervalsRef.current.delete(groupJobId); }
    const t = groupDismissTimersRef.current.get(groupJobId);
    if (t !== undefined) { clearTimeout(t); groupDismissTimersRef.current.delete(groupJobId); }
    setGroupJobs((prev) => prev.filter((n) => n.groupJobId !== groupJobId));
  }, []);

  const trackJob = useCallback((jobId: string, operation: string, device?: string) => {
    setJobs((prev) => [
      { jobId, operation, device, status: 'pending', message: 'Pending...' },
      ...prev,
    ]);

    const poll = async () => {
      try {
        const job = await getJob(jobId);

        let status: JobNotification['status'];
        let message: string;
        let operationResult: string | undefined;
        let reason: string | undefined;

        if (job.status === 'completed') {
          status = 'completed';
          message = 'Completed successfully';
          const result = job.result as { operation_result?: string; reason?: string } | null;
          operationResult = result?.operation_result;
          reason = result?.reason;
        } else if (job.status === 'failed' || job.status === 'cancelled') {
          status = 'failed';
          message =
            job.error ??
            job.last_error ??
            (job.result as { message?: string } | null)?.message ??
            'Failed';
        } else if (job.status === 'running' || job.status === 'retrying') {
          status = 'running';
          message = job.status === 'retrying' ? 'Retrying...' : 'Running...';
        } else {
          status = 'pending';
          message = 'Pending...';
        }

        setJobs((prev) =>
          prev.map((n) =>
            n.jobId === jobId ? { ...n, status, message, operationResult, reason } : n,
          ),
        );

        if (status === 'completed' || status === 'failed') {
          const iv = intervalsRef.current.get(jobId);
          if (iv !== undefined) { clearInterval(iv); intervalsRef.current.delete(jobId); }

          if (status === 'completed') {
            const t = setTimeout(() => {
              setJobs((prev) => prev.filter((n) => n.jobId !== jobId));
              dismissTimersRef.current.delete(jobId);
            }, 6000);
            dismissTimersRef.current.set(jobId, t);
          }
        }
      } catch {
        // non-fatal poll failure
      }
    };

    poll();
    const iv = setInterval(poll, 2500);
    intervalsRef.current.set(jobId, iv);
  }, []);

  const trackGroupJob = useCallback((groupJobId: string, label: string) => {
    setGroupJobs((prev) => [
      { groupJobId, label, status: 'pending', total: 0, completed: 0, failed: 0, deviceResults: [] },
      ...prev,
    ]);

    const poll = async () => {
      try {
        const gj = await getGroupJob(groupJobId);
        const { status, execution_summary, device_results } = gj;

        setGroupJobs((prev) =>
          prev.map((n) =>
            n.groupJobId === groupJobId
              ? {
                  ...n,
                  status,
                  total: execution_summary.total_devices,
                  completed: execution_summary.completed,
                  failed: execution_summary.failed,
                  deviceResults: device_results,
                }
              : n,
          ),
        );

        const isTerminal = status === 'completed' || status === 'partial_success' || status === 'failed';
        if (isTerminal) {
          const iv = groupIntervalsRef.current.get(groupJobId);
          if (iv !== undefined) { clearInterval(iv); groupIntervalsRef.current.delete(groupJobId); }

          if (status === 'completed') {
            const t = setTimeout(() => {
              setGroupJobs((prev) => prev.filter((n) => n.groupJobId !== groupJobId));
              groupDismissTimersRef.current.delete(groupJobId);
            }, 6000);
            groupDismissTimersRef.current.set(groupJobId, t);
          }
        }
      } catch {
        // non-fatal poll failure
      }
    };

    poll();
    const iv = setInterval(poll, 2500);
    groupIntervalsRef.current.set(groupJobId, iv);
  }, []);

  return (
    <JobNotificationContext.Provider value={{ jobs, groupJobs, trackJob, trackGroupJob, dismissJob, dismissGroupJob }}>
      {children}
    </JobNotificationContext.Provider>
  );
}

export function useJobNotifications(): JobNotificationContextValue {
  const ctx = useContext(JobNotificationContext);
  if (!ctx) throw new Error('useJobNotifications must be used inside JobNotificationProvider');
  return ctx;
}
