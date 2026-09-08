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

/** Poll cadence for job / group-job tracking. Kept tight (2.5s) because
 * these are user-triggered operations where the user wants immediate
 * feedback. Waste from background tabs is cut by pausing the intervals
 * on ``visibilitychange`` (see below), not by lengthening the cadence. */
const JOB_POLL_INTERVAL_MS = 2500;
/** How long a completed job notification lingers on screen before being
 * auto-dismissed. Paused while the tab is hidden so a job that finished
 * during a background stretch still gets shown for a full window when
 * the user returns. */
const DISMISS_DELAY_MS = 6000;

/** Per-poller state. ``timerId`` is undefined while paused (tab hidden). */
type PollerState = {
  poll: () => Promise<void>;
  timerId?: ReturnType<typeof setInterval>;
};

/** Per-dismiss-timer state. Tracks the remaining time on pause so that a
 * job which finishes at t=0 and then the tab goes hidden at t=1s doesn't
 * lose its full 6s viewing window -- when the tab returns, the timer is
 * restarted with the 5s that were still pending. */
type DismissTimerState = {
  callback: () => void;
  remaining: number;
  timerId?: ReturnType<typeof setTimeout>;
  lastStartedAt?: number;
};

function stopInterval(state: PollerState): void {
  if (state.timerId !== undefined) {
    clearInterval(state.timerId);
    state.timerId = undefined;
  }
}

function startInterval(state: PollerState): void {
  if (state.timerId === undefined) {
    state.timerId = setInterval(state.poll, JOB_POLL_INTERVAL_MS);
  }
}

function pauseTimer(state: DismissTimerState): void {
  if (state.timerId !== undefined) {
    clearTimeout(state.timerId);
    state.timerId = undefined;
    if (state.lastStartedAt !== undefined) {
      const elapsed = Date.now() - state.lastStartedAt;
      state.remaining = Math.max(0, state.remaining - elapsed);
    }
  }
}

function resumeTimer(state: DismissTimerState): void {
  if (state.timerId !== undefined) return; // already running
  if (state.remaining <= 0) {
    state.callback();
    return;
  }
  state.lastStartedAt = Date.now();
  state.timerId = setTimeout(state.callback, state.remaining);
}

export function JobNotificationProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<JobNotification[]>([]);
  const [groupJobs, setGroupJobs] = useState<GroupJobNotification[]>([]);

  const intervalsRef = useRef<Map<string, PollerState>>(new Map());
  const dismissTimersRef = useRef<Map<string, DismissTimerState>>(new Map());
  const groupIntervalsRef = useRef<Map<string, PollerState>>(new Map());
  const groupDismissTimersRef = useRef<Map<string, DismissTimerState>>(new Map());

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      intervalsRef.current.forEach(stopInterval);
      dismissTimersRef.current.forEach((s) => {
        if (s.timerId !== undefined) clearTimeout(s.timerId);
      });
      groupIntervalsRef.current.forEach(stopInterval);
      groupDismissTimersRef.current.forEach((s) => {
        if (s.timerId !== undefined) clearTimeout(s.timerId);
      });
    };
  }, []);

  // Decision 4 (docs/SSH_REFRESH_PLAN.md): pause polling + dismiss
  // timers when the tab is hidden; when it returns, poll every tracked
  // job immediately and resume timers with the time they had remaining.
  // React Query queries handle background suspension via
  // ``refetchIntervalInBackground: false`` (set in providers.tsx); this
  // useEffect covers the ``setInterval`` and ``setTimeout`` cases which
  // don't respect visibility on their own.
  useEffect(() => {
    if (typeof document === 'undefined') return;

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        intervalsRef.current.forEach(stopInterval);
        groupIntervalsRef.current.forEach(stopInterval);
        dismissTimersRef.current.forEach(pauseTimer);
        groupDismissTimersRef.current.forEach(pauseTimer);
      } else {
        // Visible again: catch up state immediately, then resume cadence.
        // Firing poll() before restarting the interval means the user
        // sees any status transitions that happened while the tab was
        // hidden without waiting a full cycle.
        intervalsRef.current.forEach((state) => { void state.poll(); startInterval(state); });
        groupIntervalsRef.current.forEach((state) => { void state.poll(); startInterval(state); });
        dismissTimersRef.current.forEach(resumeTimer);
        groupDismissTimersRef.current.forEach(resumeTimer);
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, []);

  const dismissJob = useCallback((jobId: string) => {
    const state = intervalsRef.current.get(jobId);
    if (state !== undefined) { stopInterval(state); intervalsRef.current.delete(jobId); }
    const timer = dismissTimersRef.current.get(jobId);
    if (timer !== undefined) {
      if (timer.timerId !== undefined) clearTimeout(timer.timerId);
      dismissTimersRef.current.delete(jobId);
    }
    setJobs((prev) => prev.filter((n) => n.jobId !== jobId));
  }, []);

  const dismissGroupJob = useCallback((groupJobId: string) => {
    const state = groupIntervalsRef.current.get(groupJobId);
    if (state !== undefined) { stopInterval(state); groupIntervalsRef.current.delete(groupJobId); }
    const timer = groupDismissTimersRef.current.get(groupJobId);
    if (timer !== undefined) {
      if (timer.timerId !== undefined) clearTimeout(timer.timerId);
      groupDismissTimersRef.current.delete(groupJobId);
    }
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
            job.error_summary ??
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
          const state = intervalsRef.current.get(jobId);
          if (state !== undefined) { stopInterval(state); intervalsRef.current.delete(jobId); }

          if (status === 'completed') {
            const dismissCb = () => {
              setJobs((prev) => prev.filter((n) => n.jobId !== jobId));
              dismissTimersRef.current.delete(jobId);
            };
            const timerState: DismissTimerState = {
              callback: dismissCb,
              remaining: DISMISS_DELAY_MS,
              lastStartedAt: Date.now(),
              timerId: setTimeout(dismissCb, DISMISS_DELAY_MS),
            };
            dismissTimersRef.current.set(jobId, timerState);
          }
        }
      } catch {
        // non-fatal poll failure
      }
    };

    const state: PollerState = { poll };
    startInterval(state);
    intervalsRef.current.set(jobId, state);
    void poll();
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
          const state = groupIntervalsRef.current.get(groupJobId);
          if (state !== undefined) { stopInterval(state); groupIntervalsRef.current.delete(groupJobId); }

          if (status === 'completed') {
            const dismissCb = () => {
              setGroupJobs((prev) => prev.filter((n) => n.groupJobId !== groupJobId));
              groupDismissTimersRef.current.delete(groupJobId);
            };
            const timerState: DismissTimerState = {
              callback: dismissCb,
              remaining: DISMISS_DELAY_MS,
              lastStartedAt: Date.now(),
              timerId: setTimeout(dismissCb, DISMISS_DELAY_MS),
            };
            groupDismissTimersRef.current.set(groupJobId, timerState);
          }
        }
      } catch {
        // non-fatal poll failure
      }
    };

    const state: PollerState = { poll };
    startInterval(state);
    groupIntervalsRef.current.set(groupJobId, state);
    void poll();
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
