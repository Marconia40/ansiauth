'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  listActiveSessions,
  revokeOtherSessions,
  type ActiveSession,
} from '@/services/api';

// Compact formatter for the "last activity" column. Absolute date is
// available in the tooltip; the column shows a rough delta so a table
// with 5+ sessions stays scannable.
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const seconds = Math.round((now - then) / 1000);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function shortenUserAgent(ua: string | null): string {
  if (!ua) return 'Unknown client';
  // Very light heuristic: pull the browser name off the tail. Full UA is
  // in the tooltip for anyone who cares.
  const match = ua.match(/(Chrome|Firefox|Safari|Edge|Opera)\/[\d.]+/);
  if (match) return match[0];
  return ua.length > 40 ? `${ua.slice(0, 40)}…` : ua;
}

export default function ActiveSessionsPage() {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [flash, setFlash] = useState<string | null>(null);

  const sessionsQuery = useQuery<ActiveSession[]>({
    queryKey: ['auth-sessions'],
    queryFn: listActiveSessions,
    // Sessions age quickly; refetch every 30s so "last activity" stays
    // approximately fresh without hammering the server.
    refetchInterval: 30_000,
  });

  const revokeMutation = useMutation({
    mutationFn: revokeOtherSessions,
    onSuccess: (revokedCount) => {
      setConfirming(false);
      setFlash(
        revokedCount === 0
          ? 'No other sessions were active.'
          : `Signed out ${revokedCount} other session${revokedCount === 1 ? '' : 's'}.`,
      );
      queryClient.invalidateQueries({ queryKey: ['auth-sessions'] });
    },
    onError: () => setFlash('Could not sign out other sessions.'),
  });

  const sessions = sessionsQuery.data ?? [];
  const others = sessions.filter((s) => !s.current);
  const disabled = revokeMutation.isPending || others.length === 0;

  return (
    <div className="max-w-3xl flex flex-col gap-4">
      <div>
        <h1 className="text-2xl font-bold text-text">Active sessions</h1>
        <p className="mt-1 text-sm text-muted">
          Every device currently signed in with your account. The row marked
          <span className="mx-1 font-semibold text-text">This device</span>
          is the one you are using right now.
        </p>
      </div>

      {sessionsQuery.isLoading && (
        <p className="text-sm text-muted">Loading sessions…</p>
      )}
      {sessionsQuery.isError && (
        <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
          Could not load sessions.
        </p>
      )}

      {sessions.length > 0 && (
        <div className="rounded-lg border border-panel-border bg-panel overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase tracking-wider text-muted bg-panel-elev">
                <th className="px-4 py-2 font-semibold">Device</th>
                <th className="px-4 py-2 font-semibold">IP</th>
                <th className="px-4 py-2 font-semibold">Signed in</th>
                <th className="px-4 py-2 font-semibold">Last activity</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr
                  key={s.session_id}
                  className="border-t border-panel-border"
                >
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span
                        className="text-text"
                        title={s.user_agent ?? ''}
                      >
                        {shortenUserAgent(s.user_agent)}
                      </span>
                      {s.current && (
                        <span className="text-xs font-semibold px-2 py-0.5 rounded-full bg-info/15 text-info border border-info/30">
                          This device
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-muted font-mono text-xs">
                    {s.ip_address ?? '—'}
                  </td>
                  <td className="px-4 py-3 text-muted text-xs">
                    <span title={new Date(s.created_at).toLocaleString()}>
                      {new Date(s.created_at).toLocaleDateString()}{' '}
                      {new Date(s.created_at).toLocaleTimeString([], {
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-muted text-xs">
                    <span title={new Date(s.last_used_at).toLocaleString()}>
                      {relativeTime(s.last_used_at)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {flash && (
        <p className="text-sm text-success border border-success/40 bg-success/10 rounded px-3 py-2">
          {flash}
        </p>
      )}

      <div className="flex items-center justify-between rounded-lg border border-panel-border bg-panel px-4 py-3">
        <div>
          <p className="text-sm font-semibold text-text">
            Sign out every other device
          </p>
          <p className="text-xs text-muted">
            Keeps you signed in here. Everything else is forced to sign in
            again immediately.
          </p>
        </div>
        {confirming ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-panel-elev"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => revokeMutation.mutate()}
              disabled={revokeMutation.isPending}
              className="rounded-md bg-danger px-3 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-50"
            >
              {revokeMutation.isPending ? 'Signing out…' : 'Yes, sign them out'}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => {
              setFlash(null);
              setConfirming(true);
            }}
            disabled={disabled}
            className="rounded-md bg-danger px-3 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Sign out other sessions
          </button>
        )}
      </div>
    </div>
  );
}
