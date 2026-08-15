'use client';

import { createContext, useContext, useState, useEffect, type ReactNode } from 'react';
import type { AuthUser } from '@/types/auth';
import { restoreSession } from '@/services/api';

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

  useEffect(() => {
    restoreSession()
      .then((restored) => {
        if (restored) setUser(restored);
      })
      .finally(() => setIsInitializing(false));
  }, []);

  return (
    <AuthContext.Provider value={{ user, isAuthenticated: user !== null, isInitializing, setUser }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
