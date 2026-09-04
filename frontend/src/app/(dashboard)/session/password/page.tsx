'use client';

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '@/context/AuthContext';
import { getUsers, updateUser } from '@/services/api';
import type { User } from '@/types/user';

export default function ChangePasswordPage() {
  const { user } = useAuth();
  const queryClient = useQueryClient();

  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Only system-admins can call GET /users and PUT /users/{id}, which is
  // currently the only path to change a password. Non-admins get a friendly
  // wall — a dedicated /auth/change-password endpoint on the backend would
  // remove that limitation.
  const canChange = Boolean(user?.is_system_admin);

  const usersQuery = useQuery<User[]>({
    queryKey: ['users'],
    queryFn: getUsers,
    enabled: canChange,
  });
  const self = usersQuery.data?.find((u) => u.username === user?.username) ?? null;

  const mutation = useMutation({
    mutationFn: async () => {
      if (!self) throw new Error('Could not locate your account.');
      return updateUser(self.id, { password: next });
    },
    onSuccess: () => {
      setSuccess('Password updated. Use the new one on your next sign-in.');
      setNext('');
      setConfirm('');
      setError(null);
      queryClient.invalidateQueries({ queryKey: ['users'] });
    },
    onError: (err: unknown) => {
      const e = err as {
        response?: { data?: { detail?: string; message?: string } };
        message?: string;
      } | null;
      setError(
        e?.response?.data?.detail ??
          e?.response?.data?.message ??
          e?.message ??
          'Password update failed.',
      );
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSuccess(null);
    if (next.length < 8) {
      setError('New password must be at least 8 characters long.');
      return;
    }
    if (next !== confirm) {
      setError('Confirmation does not match the new password.');
      return;
    }
    mutation.mutate();
  }

  return (
    <div className="max-w-md flex flex-col gap-4">
      <h1 className="text-2xl font-bold text-text">Change password</h1>

      {!canChange ? (
        <div className="rounded-md border border-warning/40 bg-warning/10 text-warning px-4 py-3 text-sm">
          Only system administrators can change passwords through the UI in this
          release. Ask your admin to reset your password. (A dedicated
          <code className="mx-1 rounded bg-panel-elev px-1 py-0.5 text-text">
            /auth/change-password
          </code>
          endpoint on the backend would let you do it yourself.)
        </div>
      ) : (
        <form
          onSubmit={handleSubmit}
          className="flex flex-col gap-3 rounded-lg border border-panel-border bg-panel p-5"
        >
          <p className="text-sm text-muted">
            Signed in as <span className="font-semibold text-text">{user?.username}</span>.
            The change applies immediately — you stay signed in on this tab.
          </p>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wider text-muted">
              New password
            </span>
            <input
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
              className="rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              required
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-xs uppercase tracking-wider text-muted">
              Confirm new password
            </span>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              className="rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              required
            />
          </label>

          {error && (
            <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
              {error}
            </p>
          )}
          {success && (
            <p className="text-sm text-success border border-success/40 bg-success/10 rounded px-3 py-2">
              {success}
            </p>
          )}

          <div className="flex items-center justify-end gap-2">
            <button
              type="submit"
              disabled={mutation.isPending || usersQuery.isLoading}
              className="rounded-md bg-info px-4 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              {mutation.isPending ? 'Saving…' : 'Update password'}
            </button>
          </div>

          {usersQuery.isError && (
            <p className="text-xs text-danger">
              Could not load your account — password change is unavailable.
            </p>
          )}
        </form>
      )}
    </div>
  );
}
