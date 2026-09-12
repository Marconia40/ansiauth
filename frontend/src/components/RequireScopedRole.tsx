'use client';

import type { ReactNode } from 'react';
import { useCanPerform, type ScopeCoords } from '@/hooks/useAuthz';
import type { Op } from '@/constants/opMinRole';

interface Props {
  op: Op;
  /** ``null`` means the scope hasn't been resolved yet (async lookup
   * in flight) — treated as "not allowed" until it lands. */
  scope: ScopeCoords | null;
  children: ReactNode;
  fallback?: ReactNode;
}

/**
 * Gates children on whether the current caller may perform ``op`` at
 * ``scope`` — max-role resolved from their grants, matching the
 * backend's ``VisibilityScope.rol_para()``. UX-only: the backend
 * re-checks every mutation. See docs/USER_PERMISSIONS_UX_REDESIGN.md
 * §3.2.
 *
 * While grants are still loading the children are hidden — flashing a
 * button and then hiding it if the user turns out not to have access
 * is worse than the reverse.
 */
export function RequireScopedRole({
  op,
  scope,
  children,
  fallback = null,
}: Props) {
  const { allowed } = useCanPerform(op, scope);
  return <>{allowed ? children : fallback}</>;
}
