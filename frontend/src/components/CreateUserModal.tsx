'use client';

import { useState } from 'react';
import { createUser, extractMessage } from '@/services/api';
import type { User } from '@/types/user';
import { ErrorMessage } from '@/components/ErrorMessage';

interface CreateUserModalProps {
  onClose: () => void;
  onCreated: (user: User) => void;
}

/**
 * Step 1 of the two-step create flow (§3.4). Captures only credentials
 * and creates the user with ``is_system_admin=false`` and zero grants.
 * The parent hands the returned user to ``ManageUserModal`` for step 2,
 * where per-scope grants and (for system-admin viewers) the system-wide
 * toggle are applied.
 *
 * Deliberately no role dropdown here — the legacy version mapped
 * observer/operator to "user with no access" and admin/super-admin to
 * ``is_system_admin=true``, which mixed two very different intents.
 * Both intents now live in step 2.
 */
export function CreateUserModal({ onClose, onCreated }: CreateUserModalProps) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password.trim()) {
      setError('Username and password are required');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const user = await createUser({
        username: username.trim(),
        password: password.trim(),
        email: email.trim() || undefined,
        is_system_admin: false,
      });
      onCreated(user);
    } catch (err) {
      setError(extractMessage(err, 'Create failed'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 overflow-y-auto py-8">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md bg-panel rounded-lg shadow-xl p-6 space-y-4"
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-text">Create user</h2>
            <p className="text-xs text-muted mt-1">
              Just credentials for now — configure access in the next step.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="text-sm text-muted hover:text-text disabled:opacity-50"
          >
            Cancel
          </button>
        </div>

        {error && <ErrorMessage error={`✗ ${error}`} />}

        <div className="flex flex-col gap-3">
          <div className="flex flex-col">
            <label className="text-xs text-muted mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={busy}
              required
              autoFocus
              className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
            />
          </div>
          <div className="flex flex-col">
            <label className="text-xs text-muted mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={busy}
              required
              className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
            />
          </div>
          <div className="flex flex-col">
            <label className="text-xs text-muted mb-1">Email (optional)</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
              className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
            />
          </div>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            className="px-3 py-1.5 text-sm text-muted border border-panel-border rounded-md hover:bg-panel-elev/60 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={busy || !username.trim() || !password.trim()}
            className="px-3 py-1.5 text-sm text-white bg-info rounded-md hover:bg-info disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {busy ? 'Creating…' : 'Create and configure access →'}
          </button>
        </div>
      </form>
    </div>
  );
}
