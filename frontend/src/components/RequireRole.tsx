'use client';

import type { ReactNode } from 'react';
import { useAuth } from '@/context/AuthContext';
import type { Role } from '@/types/auth';

const ROLE_LEVEL: Record<Role, number> = {
  observer: 1,
  operator: 2,
  admin: 3,
  'super-admin': 4,
};

interface Props {
  roles: Role[];
  children: ReactNode;
  fallback?: ReactNode;
}

// Renders children only when the logged-in user has one of the required roles.
// Use this to hide buttons or sections the user can't act on anyway.
// The backend enforces real authorization — this is UX only.
export function RequireRole({ roles, children, fallback = null }: Props) {
  const { user } = useAuth();
  if (!user || !roles.includes(user.role)) return <>{fallback}</>;
  return <>{children}</>;
}

// Returns true when the user's role meets or exceeds the minimum required level.
export function useHasRole(minRole: Role): boolean {
  const { user } = useAuth();
  if (!user) return false;
  return ROLE_LEVEL[user.role] >= ROLE_LEVEL[minRole];
}
