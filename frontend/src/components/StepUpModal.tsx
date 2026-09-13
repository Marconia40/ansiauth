'use client';

import { useEffect, useRef, useState } from 'react';
import { useAuth } from '@/context/AuthContext';

interface StepUpModalProps {
  submitting: boolean;
  error: string | null;
  onSubmit: (password: string) => void;
  onCancel: () => void;
}

/**
 * Password prompt shown when a sensitive endpoint returned
 * ``reauth_required``. Purposefully bare: one field, one Confirm, one
 * Cancel. Autofocuses the input and traps Esc to Cancel so a reflexive
 * Esc doesn't get stuck behind the modal.
 */
export function StepUpModal({ submitting, error, onSubmit, onCancel }: StepUpModalProps) {
  const { user } = useAuth();
  const [password, setPassword] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!password || submitting) return;
    onSubmit(password);
  }

  return (
    <div
      className="fixed inset-0 z-[110] flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="stepup-title"
    >
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-panel rounded-lg shadow-xl p-6 space-y-4 mx-4"
      >
        <div>
          <h2 id="stepup-title" className="text-lg font-semibold text-text">
            Confirm your password
          </h2>
          <p className="mt-2 text-sm text-muted">
            This action needs an extra security check. Re-enter the password
            for{' '}
            <span className="font-semibold text-text">
              {user?.username ?? 'your account'}
            </span>{' '}
            to continue.
          </p>
        </div>

        <label className="flex flex-col gap-1">
          <span className="text-xs uppercase tracking-wider text-muted">Password</span>
          <input
            ref={inputRef}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            className="rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            required
          />
        </label>

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onCancel}
            disabled={submitting}
            className="px-3 py-1.5 text-sm text-muted hover:bg-panel-elev rounded-md transition-colors disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitting || !password}
            className="px-3 py-1.5 text-sm font-semibold text-white bg-info rounded-md hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shadow-sm"
          >
            {submitting ? 'Confirming…' : 'Confirm'}
          </button>
        </div>
      </form>
    </div>
  );
}
