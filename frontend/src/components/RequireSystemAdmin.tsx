'use client';

import type { ReactNode } from 'react';
import { useAuth } from '@/context/AuthContext';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

/**
 * Gates children on the caller's ``is_system_admin`` bit. For actions
 * that the backend enforces with ``require_system_admin`` — creating
 * or deleting a Site, toggling the /system-admin flag on another
 * user, unlocking a device — the operation is inherently global, so
 * the per-scope model doesn't apply.
 *
 * Use ``RequireScopedRole`` for anything the backend gates with
 * ``require_scope("<op>")`` — those *are* per-site.
 */
export function RequireSystemAdmin({ children, fallback = null }: Props) {
  const { user } = useAuth();
  if (!user?.is_system_admin) return <>{fallback}</>;
  return <>{children}</>;
}
