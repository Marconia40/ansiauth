'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { reauth, registerElevatedTokenProvider } from '@/services/api';
import { StepUpModal } from '@/components/StepUpModal';

/**
 * Step-up context. Manages the single active password-prompt modal that
 * the 401 interceptor triggers when a sensitive endpoint returns
 * ``detail="reauth_required"``.
 *
 * Concurrency: multiple sensitive requests can fail in parallel (e.g.
 * bulk delete). We share one pending promise across all of them so the
 * user sees a single modal and every waiter resolves once the password
 * is verified. The modal is torn down whether the user submits (success),
 * mistypes then submits again, or cancels — cancellation rejects every
 * waiter so callers can show a "action cancelled" toast rather than
 * appearing to hang.
 */

interface StepUpContextValue {
  /**
   * Programmatic trigger — mostly for testing or ad-hoc callers.
   * Production code lets the axios interceptor call this via the
   * registered provider (see registerElevatedTokenProvider).
   */
  requireElevated: () => Promise<string>;
}

const StepUpContext = createContext<StepUpContextValue | null>(null);

type Waiter = {
  resolve: (token: string) => void;
  reject: (reason: unknown) => void;
};

export function StepUpProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const waitersRef = useRef<Waiter[]>([]);

  const requireElevated = useCallback((): Promise<string> => {
    return new Promise<string>((resolve, reject) => {
      waitersRef.current.push({ resolve, reject });
      setError(null);
      setOpen(true);
    });
  }, []);

  // Register with the api service so its 401 interceptor can call the
  // modal automatically on ``reauth_required``.
  useEffect(() => registerElevatedTokenProvider(requireElevated), [requireElevated]);

  const settleAll = useCallback(
    (settle: (w: Waiter) => void) => {
      const drained = waitersRef.current;
      waitersRef.current = [];
      drained.forEach(settle);
    },
    [],
  );

  const handleSubmit = useCallback(
    async (password: string) => {
      setSubmitting(true);
      setError(null);
      try {
        await reauth(password);
        setOpen(false);
        // The token was just cached in ``api.ts``; return the resolved
        // waiters a marker string. The interceptor re-reads the cached
        // token from ``isElevatedTokenValid`` on the retry rather than
        // trusting this string, so the value doesn't have to be real.
        settleAll((w) => w.resolve('ok'));
      } catch (err) {
        const detail =
          (err as { response?: { data?: { detail?: string; message?: string } } })?.response?.data
            ?.detail ??
          (err as { response?: { data?: { message?: string } } })?.response?.data?.message ??
          'Incorrect password';
        setError(String(detail));
      } finally {
        setSubmitting(false);
      }
    },
    [settleAll],
  );

  const handleCancel = useCallback(() => {
    setOpen(false);
    settleAll((w) => w.reject(new Error('reauth_cancelled')));
  }, [settleAll]);

  return (
    <StepUpContext.Provider value={{ requireElevated }}>
      {children}
      {open && (
        <StepUpModal
          submitting={submitting}
          error={error}
          onSubmit={handleSubmit}
          onCancel={handleCancel}
        />
      )}
    </StepUpContext.Provider>
  );
}

export function useStepUp(): StepUpContextValue {
  const ctx = useContext(StepUpContext);
  if (!ctx) throw new Error('useStepUp must be used inside StepUpProvider');
  return ctx;
}
