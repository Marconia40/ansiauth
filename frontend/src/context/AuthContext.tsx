'use client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from 'react';
import type { AuthUser } from '@/types/auth';
import {
  onSessionExpired,
  registerSessionRevalidation,
  restoreSession,
  signOutClientIdle,
} from '@/services/api';
import {
  broadcastLogout,
  initSessionActivity,
  markActivity,
  onRemoteLogout,
} from '@/lib/sessionActivity';
import { useIdleTimeout } from '@/hooks/useIdleTimeout';
import { IdleWarningModal } from '@/components/IdleWarningModal';

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isInitializing: boolean;
  setUser: (user: AuthUser | null) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isInitializing, setIsInitializing] = useState(true);
  const [warningSeconds, setWarningSeconds] = useState<number | null>(null);

  useEffect(() => {
    restoreSession()
      .then((restored) => {
        if (restored) setUser(restored);
      })
      .finally(() => setIsInitializing(false));
  }, []);

  useEffect(() => {
    const unsubscribe = onSessionExpired(() => {
      setUser(null);
      setWarningSeconds(null);
      if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
        window.location.replace('/login');
      }
    });
    return unsubscribe;
  }, []);

  useEffect(() => registerSessionRevalidation(), []);

  // Cross-tab activity + logout channel. Wired unconditionally so a tab
  // that receives a remote-logout signal reacts even when it happens to
  // have ``user=null`` in memory (e.g. mid-refresh).
  useEffect(() => initSessionActivity(), []);
  useEffect(
    () =>
      onRemoteLogout(() => {
        setUser(null);
        setWarningSeconds(null);
        if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
          window.location.replace('/login');
        }
      }),
    [],
  );

  const handleIdle = useCallback(() => {
    setWarningSeconds(null);
    broadcastLogout();
    void signOutClientIdle();
  }, []);
  const handleWarn = useCallback((seconds: number) => setWarningSeconds(seconds), []);
  const handleResume = useCallback(() => setWarningSeconds(null), []);

  useIdleTimeout(user !== null, {
    onIdle: handleIdle,
    onWarn: handleWarn,
    onResume: handleResume,
  });

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: user !== null, isInitializing, setUser }}>
      {children}
      {warningSeconds !== null && (
        <IdleWarningModal
          secondsRemaining={warningSeconds}
          onStay={() => {
            markActivity('local');
            setWarningSeconds(null);
          }}
          onLogoutNow={handleIdle}
        />
      )}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
