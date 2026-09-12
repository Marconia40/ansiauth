'use client';

import { useQuery } from '@tanstack/react-query';
import { listGrants } from '@/services/api';
import { useAuth } from '@/context/AuthContext';
import { OP_MIN_ROLE, ROLE_LEVEL, type Op, type AssignmentRole } from '@/constants/opMinRole';
import type { RoleAssignment } from '@/types/user';

export type ScopeCoords =
  | { siteId: number; deviceGroupId?: number | null };

/**
 * Fetch and cache the current caller's grants. Powers every per-scope
 * gate in the app; sharing one React Query key means N gates on the
 * same page issue one HTTP request, not N.
 *
 * Disabled when the JWT payload doesn't carry ``id`` (pre-Phase-5 token
 * still in play, or a fixture / test session) — gates fall back to the
 * ``is_system_admin`` bit alone in that case (safe: non-admins see
 * nothing above observer, admins bypass).
 */
export function useMyGrants() {
  const { user } = useAuth();
  const enabled = user != null && typeof user.id === 'number' && !user.is_system_admin;
  return useQuery<RoleAssignment[]>({
    queryKey: ['grants-self', user?.id],
    queryFn: () => listGrants(user!.id!),
    enabled,
    staleTime: 60 * 1000,
  });
}

/**
 * Max-role resolution — mirrors ``VisibilityScope.rol_para()`` in
 * ``backend/app/models/visibility_scope.py`` (see D11 in
 * MSP_IMPLEMENTATION_PLAN.md). Grants are additive: the effective
 * role at a group is the highest of the site-wide grant and the
 * group-specific grant that apply to it. When called with
 * ``deviceGroupId`` null/undefined, returns the site-wide grant only —
 * a group-scoped admin is deliberately NOT counted as a site-admin.
 */
function effectiveRole(
  grants: RoleAssignment[],
  siteId: number,
  deviceGroupId: number | null | undefined,
): AssignmentRole | null {
  let siteWide: AssignmentRole | null = null;
  let groupSpecific: AssignmentRole | null = null;
  for (const g of grants) {
    if (g.site_id !== siteId) continue;
    if (g.device_group_id === null) {
      siteWide = g.role;
    } else if (deviceGroupId != null && g.device_group_id === deviceGroupId) {
      groupSpecific = g.role;
    }
  }
  if (deviceGroupId == null) return siteWide;
  const candidates = [siteWide, groupSpecific].filter(
    (r): r is AssignmentRole => r !== null,
  );
  if (candidates.length === 0) return null;
  return candidates.reduce((best, r) =>
    ROLE_LEVEL[r] > ROLE_LEVEL[best] ? r : best,
  );
}

/**
 * True when the caller could plausibly perform *some* site-scoped
 * admin operation (create group, edit site) somewhere in the system.
 * Used to gate top-of-page forms that pick a target site from a
 * dropdown: the form itself has no scope yet, but if the caller has
 * admin on zero sites there's no point showing it.
 */
export function useHasAnyAdminSite(): { loading: boolean; allowed: boolean } {
  const { user } = useAuth();
  const grantsQuery = useMyGrants();
  if (user?.is_system_admin) return { loading: false, allowed: true };
  if (grantsQuery.isLoading) return { loading: true, allowed: false };
  const anySiteAdmin = (grantsQuery.data ?? []).some(
    (g) => g.role === 'admin' && g.device_group_id === null,
  );
  return { loading: false, allowed: anySiteAdmin };
}

export interface CanPerformResult {
  loading: boolean;
  /**
   * True when the caller can perform ``op`` at ``scope``. False while
   * grants are still loading — hiding a button briefly and then
   * revealing it once permissions are known is safer than the reverse.
   */
  allowed: boolean;
  /**
   * The resolved role at the scope, useful for consumers that want to
   * show *why* something is disabled (e.g. "you are observer here").
   * ``null`` when the caller has no grant on the scope.
   */
  effectiveRole: AssignmentRole | 'super-admin' | null;
}

/**
 * Returns whether the current caller may perform ``op`` at ``scope``.
 * The backend re-checks every request; this is UX-only — hides
 * buttons the caller would 403 on. See
 * ``docs/USER_PERMISSIONS_UX_REDESIGN.md`` §3.2.
 */
export function useCanPerform(op: Op, scope: ScopeCoords): CanPerformResult {
  const { user } = useAuth();
  const grantsQuery = useMyGrants();
  const def = OP_MIN_ROLE[op];

  if (user == null) {
    return { loading: false, allowed: false, effectiveRole: null };
  }
  if (user.is_system_admin) {
    return { loading: false, allowed: true, effectiveRole: 'super-admin' };
  }
  if (grantsQuery.isLoading) {
    return { loading: true, allowed: false, effectiveRole: null };
  }
  const grants = grantsQuery.data ?? [];
  // Use the group id only when the op is scoped at group or device
  // level — for a ``site`` op the max-role helper's site-wide-only
  // branch is what we want (matches how the backend calls
  // ``rol_para(site_id, None)`` for site-scoped ops).
  const groupIdForResolution =
    def.scope_kind === 'site' ? null : scope.deviceGroupId ?? null;
  const role = effectiveRole(grants, scope.siteId, groupIdForResolution);
  const allowed = role != null && ROLE_LEVEL[role] >= ROLE_LEVEL[def.min_role];
  return { loading: false, allowed, effectiveRole: role };
}
