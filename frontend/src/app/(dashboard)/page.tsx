'use client';

import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { StatusBadge } from '@/components/StatusBadge';
import { ElapsedTimer } from '@/components/ElapsedTimer';
import { useAuth } from '@/context/AuthContext';
import { getDevices, getJobs, getAuditLogs } from '@/services/api';
import { ACTIVE_JOB_STATUSES } from '@/types/job';
import type { Device } from '@/types/device';
import type { Job } from '@/types/job';
import type { AuditLog } from '@/types/audit';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

function statusColor(status: string): string {
  if (status === 'completed' || status === 'success') return 'text-green-600';
  if (status === 'failed' || status === 'error' || status === 'cancelled') return 'text-red-600';
  if ((ACTIVE_JOB_STATUSES as string[]).includes(status)) return 'text-amber-600';
  if (status === 'partial_failure' || status === 'partial_success' || status === 'rollback_performed') return 'text-orange-600';
  return 'text-gray-600';
}

function formatDuration(job: Job): string {
  if (job.started_at && job.finished_at) {
    const ms = new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
    return `${(ms / 1000).toFixed(1)}s`;
  }
  return '—';
}

function formatDate(ts: string | null | undefined): string {
  if (!ts) return 'N/A';
  return new Date(ts).toLocaleString();
}

export default function DashboardPage() {
  const { user } = useAuth();

  const {
    data: devices,
    isLoading: devicesLoading,
    isFetching: devicesFetching,
    error: devicesError,
    refetch: refetchDevices,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const {
    data: jobsRaw,
    isLoading: jobsLoading,
    isFetching: jobsFetching,
    error: jobsError,
    refetch: refetchJobs,
  } = useQuery({
    queryKey: ['dashboard-jobs'],
    queryFn: () => getJobs({ page: 1, page_size: 100 }),
  });

  const {
    data: auditLogs,
    isLoading: auditLoading,
    isFetching: auditFetching,
    error: auditError,
    refetch: refetchAudit,
  } = useQuery<AuditLog[]>({
    queryKey: ['dashboard-audit'],
    queryFn: () => getAuditLogs({ limit: 50 }),
  });

  const isFetching = devicesFetching || jobsFetching || auditFetching;
  const isLoading = devicesLoading || jobsLoading || auditLoading;

  function handleRefresh() {
    refetchDevices();
    refetchJobs();
    refetchAudit();
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-24">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  const firstError = devicesError || jobsError;
  if (firstError) {
    return (
      <div className="py-8">
        <ErrorMessage error={extractMessage(firstError, 'Could not load dashboard data')} />
        <button
          onClick={handleRefresh}
          className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
        >
          Retry
        </button>
      </div>
    );
  }

  const deviceList = devices ?? [];
  const jobs = (Array.isArray(jobsRaw) ? jobsRaw : []) as Job[];
  const logs = auditLogs ?? [];

  const totalDevices = deviceList.length;
  const ciscoDevices = deviceList.filter((d) => d.vendor.toLowerCase().includes('cisco')).length;
  const huaweiDevices = deviceList.filter((d) => d.vendor.toLowerCase().includes('huawei')).length;
  const totalJobs = jobs.length;
  const failedJobs = jobs.filter((j) => j.status === 'failed').length;
  const runningJobs = jobs.filter((j) => j.status === 'running' || j.status === 'pending').length;
  const completedJobs = jobs.filter((j) => j.status === 'completed').length;
  const finishedJobs = jobs.filter(
    (j) => j.status !== 'pending' && j.status !== 'running' && j.status !== 'retrying',
  ).length;
  const successRate = finishedJobs === 0 ? 'N/A' : `${Math.round((completedJobs / finishedJobs) * 100)}%`;
  const auditCount = auditError ? null : logs.length;

  const recentJobs = jobs.slice(0, 5);
  const recentLogs = logs.slice(0, 5);

  return (
    <div>
      <PageHeader
        title="Dashboard"
        actions={
          <button
            onClick={handleRefresh}
            disabled={isFetching}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-8">System overview and automation activity</p>

      {/* Overview cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-10">
        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Total Devices</p>
          <p className="text-3xl font-semibold text-gray-900 mt-1">{totalDevices}</p>
          <p className="text-xs text-gray-400 mt-1">Managed devices</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Cisco Devices</p>
          <p className="text-3xl font-semibold text-gray-900 mt-1">{ciscoDevices}</p>
          <p className="text-xs text-gray-400 mt-1">Cisco-managed devices</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Huawei Devices</p>
          <p className="text-3xl font-semibold text-gray-900 mt-1">{huaweiDevices}</p>
          <p className="text-xs text-gray-400 mt-1">Huawei-managed devices</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Total Jobs</p>
          <p className="text-3xl font-semibold text-gray-900 mt-1">{totalJobs}</p>
          <p className="text-xs text-gray-400 mt-1">Recorded automation jobs</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Failed Jobs</p>
          <p className={`text-3xl font-semibold mt-1 ${failedJobs > 0 ? 'text-red-600' : 'text-gray-900'}`}>
            {failedJobs}
          </p>
          <p className="text-xs text-gray-400 mt-1">Failed executions</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Running Jobs</p>
          <p className={`text-3xl font-semibold mt-1 ${runningJobs > 0 ? 'text-amber-600' : 'text-gray-900'}`}>
            {runningJobs}
          </p>
          <p className="text-xs text-gray-400 mt-1">In progress</p>
        </div>

        <div className="border border-gray-200 rounded-md p-4">
          <p className="text-xs text-gray-500 uppercase tracking-wide">Success Rate</p>
          <p className="text-3xl font-semibold text-gray-900 mt-1">{successRate}</p>
          <p className="text-xs text-gray-400 mt-1">Completed jobs success rate</p>
        </div>

        {auditCount !== null && (
          <div className="border border-gray-200 rounded-md p-4">
            <p className="text-xs text-gray-500 uppercase tracking-wide">Audit Events</p>
            <p className="text-3xl font-semibold text-gray-900 mt-1">{auditCount}</p>
            <p className="text-xs text-gray-400 mt-1">Recent audit entries</p>
          </div>
        )}
      </div>

      {/* Quick Actions */}
      <div className="mb-10">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Quick Actions</h2>
        <div className="flex flex-wrap gap-2">
          <Link
            href="/vlans"
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Go to VLANs
          </Link>
          <Link
            href="/devices"
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Go to Devices
          </Link>
          <Link
            href="/jobs"
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Go to Jobs
          </Link>
          {(user?.role === 'admin' || user?.role === 'super-admin') && (
            <Link
              href="/users"
              className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
            >
              Go to Users
            </Link>
          )}
          <Link
            href="/device-groups"
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Go to Device Groups
          </Link>
        </div>
      </div>

      {/* Recent Jobs */}
      <div className="mb-10">
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Recent Jobs</h2>
        {recentJobs.length === 0 ? (
          <p className="text-sm text-gray-400 py-4">No jobs executed yet.</p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50">
                <th className="text-left px-4 py-2 font-medium text-gray-700">Job ID</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Operation</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Device</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Status</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Duration</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Created</th>
              </tr>
            </thead>
            <tbody>
              {recentJobs.map((job) => {
                const isActive = (ACTIVE_JOB_STATUSES as string[]).includes(job.status);
                return (
                  <tr key={job.job_id} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="px-4 py-2 font-mono text-xs text-gray-700">{job.job_id.slice(0, 8)}…</td>
                    <td className="px-4 py-2 text-gray-900">{job.playbook ?? '—'}</td>
                    <td className="px-4 py-2 text-gray-900">{job.device ?? '—'}</td>
                    <td className="px-4 py-2">
                      <StatusBadge status={job.status} />
                    </td>
                    <td className="px-4 py-2 text-gray-600">
                      {isActive
                        ? <ElapsedTimer startedAt={job.started_at} className="text-xs text-amber-600" />
                        : formatDuration(job)}
                    </td>
                    <td className="px-4 py-2 text-gray-600">{formatDate(job.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Recent Activity — only shown to roles with audit access */}
      {!auditError && (
      <div>
        <h2 className="text-sm font-semibold text-gray-700 mb-3">Recent Activity</h2>
        {recentLogs.length === 0 ? (
          <p className="text-sm text-gray-400 py-4">No audit activity yet.</p>
        ) : (
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50">
                <th className="text-left px-4 py-2 font-medium text-gray-700 whitespace-nowrap">Timestamp</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">User</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Action</th>
                <th className="text-left px-4 py-2 font-medium text-gray-700">Status</th>
              </tr>
            </thead>
            <tbody>
              {recentLogs.map((log) => (
                <tr key={log.id} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-600 text-xs whitespace-nowrap">
                    {new Date(log.timestamp).toLocaleString()}
                  </td>
                  <td className="px-4 py-2 text-gray-900">{log.user}</td>
                  <td className="px-4 py-2 font-mono text-xs text-gray-900">{log.action}</td>
                  <td className={`px-4 py-2 font-medium ${statusColor(log.status)}`}>{log.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      )}
    </div>
  );
}
