'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getJobs, getJob } from '@/services/api';
import type { Job, JobStatus } from '@/types/job';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

function statusClass(status: JobStatus | string): string {
  if (status === 'completed') return 'text-green-600';
  if (status === 'failed' || status === 'cancelled') return 'text-red-600';
  if (status === 'running' || status === 'retrying' || status === 'pending') return 'text-amber-600';
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

function normalizeJobs(data: unknown): Job[] {
  if (Array.isArray(data)) return data as Job[];
  if (data && typeof data === 'object') {
    const d = data as Record<string, unknown>;
    if (Array.isArray(d.items)) return d.items as Job[];
    if (Array.isArray(d.jobs)) return d.jobs as Job[];
  }
  return [];
}

export default function JobsPage() {
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const {
    data: jobsRaw,
    isLoading: jobsLoading,
    error: jobsError,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ['jobs'],
    queryFn: () => getJobs(),
    refetchInterval: (query) => {
      const jobs = normalizeJobs(query.state.data);
      const hasActive = jobs.some((j) => j.status === 'pending' || j.status === 'running');
      return hasActive ? 5000 : false;
    },
  });

  const jobs = normalizeJobs(jobsRaw);

  const {
    data: selectedJob,
    isLoading: detailLoading,
    error: detailError,
  } = useQuery({
    queryKey: ['job', selectedJobId],
    queryFn: () => getJob(selectedJobId!),
    enabled: !!selectedJobId,
    refetchInterval: (query) => {
      const job = query.state.data as Job | undefined;
      if (!job) return false;
      return (job.status === 'pending' || job.status === 'running') ? 5000 : false;
    },
  });

  const detail = selectedJob as Job | undefined;

  return (
    <div>
      <PageHeader
        title="Jobs"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isFetching}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">View network automation job execution</p>

      {jobsLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : jobsError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(jobsError, 'Could not load jobs')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : jobs.length === 0 ? (
        <p className="py-12 text-center text-gray-400 text-sm">No jobs executed yet.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700">Job ID</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Playbook</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Device</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Status</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Retries</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Rollback</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Error</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Duration</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Created</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => (
              <tr
                key={job.job_id}
                onClick={() =>
                  setSelectedJobId(job.job_id === selectedJobId ? null : job.job_id)
                }
                className={`border-b border-gray-100 cursor-pointer hover:bg-blue-50 transition-colors ${
                  job.job_id === selectedJobId ? 'bg-blue-50' : ''
                }`}
              >
                <td className="px-4 py-2 font-mono text-xs text-gray-700">
                  {job.job_id.slice(0, 8)}…
                </td>
                <td className="px-4 py-2 text-gray-900">{job.playbook ?? '—'}</td>
                <td className="px-4 py-2 text-gray-900">{job.device ?? '—'}</td>
                <td className={`px-4 py-2 font-medium ${statusClass(job.status)}`}>
                  {job.status}
                </td>
                <td className="px-4 py-2 text-gray-600">
                  {job.retry_count} / {job.max_retries}
                </td>
                <td className="px-4 py-2 text-gray-600">
                  {job.rollback_performed ? 'Yes' : 'No'}
                </td>
                <td className="px-4 py-2 text-gray-600 max-w-[160px] truncate">
                  {job.error ?? job.last_error ?? '—'}
                </td>
                <td className="px-4 py-2 text-gray-600">{formatDuration(job)}</td>
                <td className="px-4 py-2 text-gray-600">{formatDate(job.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedJobId && (
        <div className="mt-8 border border-gray-200 rounded-md p-6 bg-gray-50">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-gray-700">Job Detail</h2>
            <button
              onClick={() => setSelectedJobId(null)}
              className="text-xs text-gray-400 hover:text-gray-600"
            >
              Close
            </button>
          </div>

          {detailLoading ? (
            <LoadingSpinner size="sm" />
          ) : detailError ? (
            <ErrorMessage error={extractMessage(detailError, 'Could not load job detail')} />
          ) : detail ? (
            <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2 text-sm">
              <dt className="text-gray-500">Job ID</dt>
              <dd className="font-mono text-gray-900 break-all">{detail.job_id}</dd>

              <dt className="text-gray-500">Playbook</dt>
              <dd className="text-gray-900">{detail.playbook ?? 'N/A'}</dd>

              <dt className="text-gray-500">Status</dt>
              <dd className={`font-medium ${statusClass(detail.status)}`}>{detail.status}</dd>

              <dt className="text-gray-500">Device</dt>
              <dd className="text-gray-900">{detail.device ?? 'N/A'}</dd>

              <dt className="text-gray-500">Current Step</dt>
              <dd className="text-gray-900">{detail.current_step ?? 'N/A'}</dd>

              <dt className="text-gray-500">Retry Count</dt>
              <dd className="text-gray-900">{detail.retry_count} / {detail.max_retries}</dd>

              <dt className="text-gray-500">Rollback Performed</dt>
              <dd className="text-gray-900">{detail.rollback_performed ? 'Yes' : 'No'}</dd>

              <dt className="text-gray-500">Error</dt>
              <dd className="text-red-600">{detail.error ?? '—'}</dd>

              <dt className="text-gray-500">Last Error</dt>
              <dd className="text-red-600">{detail.last_error ?? '—'}</dd>

              <dt className="text-gray-500">Started At</dt>
              <dd className="text-gray-900">{formatDate(detail.started_at)}</dd>

              <dt className="text-gray-500">Finished At</dt>
              <dd className="text-gray-900">{formatDate(detail.finished_at)}</dd>

              <dt className="text-gray-500">Created At</dt>
              <dd className="text-gray-900">{formatDate(detail.created_at)}</dd>

              {detail.pre_state != null && (
                <>
                  <dt className="text-gray-500 pt-1">Pre-State</dt>
                  <dd>
                    <pre className="text-xs text-gray-700 bg-white border border-gray-200 rounded p-2 overflow-auto max-h-40">
                      {JSON.stringify(detail.pre_state, null, 2)}
                    </pre>
                  </dd>
                </>
              )}
            </dl>
          ) : null}
        </div>
      )}
    </div>
  );
}
