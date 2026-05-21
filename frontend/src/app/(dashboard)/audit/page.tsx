'use client';

import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getAuditLogs } from '@/services/api';
import type { AuditLog } from '@/types/audit';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.message ?? fallback;
}

function formatTimestamp(ts: string): string {
  return new Date(ts).toLocaleString();
}

function statusClass(status: string): string {
  if (status === 'success' || status === 'completed') return 'text-green-600';
  if (status === 'failed' || status === 'error') return 'text-red-600';
  if (status === 'pending' || status === 'running') return 'text-amber-600';
  return 'text-gray-600';
}

export default function AuditPage() {
  const {
    data: logs,
    isLoading,
    error,
    refetch,
    isFetching,
  } = useQuery<AuditLog[]>({
    queryKey: ['audit-logs'],
    queryFn: () => getAuditLogs({ limit: 100 }),
  });

  return (
    <div>
      <PageHeader
        title="Audit Logs"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing…' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">View system audit history</p>

      {isLoading ? (
        <div className="py-12 flex justify-center">
          <LoadingSpinner size="lg" />
        </div>
      ) : error ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(error, 'Could not load audit logs')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : !logs || logs.length === 0 ? (
        <p className="py-12 text-center text-gray-400 text-sm">No audit logs found.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700 whitespace-nowrap">Timestamp</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">User</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Action</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Resource Type</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Status</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Details</th>
            </tr>
          </thead>
          <tbody>
            {logs.map((log) => (
              <tr key={log.id} className="border-b border-gray-100 hover:bg-gray-50 align-top">
                <td className="px-4 py-2 text-gray-600 whitespace-nowrap text-xs">
                  {formatTimestamp(log.timestamp)}
                </td>
                <td className="px-4 py-2 text-gray-900">{log.user}</td>
                <td className="px-4 py-2 font-mono text-xs text-gray-900">{log.action}</td>
                <td className="px-4 py-2 text-gray-600">{log.resource}</td>
                <td className={`px-4 py-2 font-medium ${statusClass(log.status)}`}>
                  {log.status}
                </td>
                <td className="px-4 py-2">
                  <pre className="text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded p-2 max-w-xs overflow-auto max-h-32 whitespace-pre-wrap">
                    {JSON.stringify(log.details, null, 2)}
                  </pre>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
