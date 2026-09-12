/**
 * TypeScript mirror of ``backend/app/core/scope.py::OP_MIN_ROLE``.
 *
 * The backend is the authoritative source; this file only powers UI
 * gates (hide/show buttons) so operators don't click affordances that
 * would 403. The backend re-checks every request — a stale mirror will
 * only ever cause UI drift (a hidden button that could have been
 * shown, or vice versa), never a real authorization gap.
 *
 * **Rule of thumb.** Any change to the backend matrix must land in
 * this file in the same PR. Adding an op only in the backend leaves
 * the UI unable to gate on it; adding only here yields ghost
 * affordances.
 *
 * Ops that use ``require_system_admin`` on the backend (site create,
 * site delete, /system-admin toggle, unlock) do **not** appear in the
 * backend matrix and do not belong here either — they are gated with
 * ``RequireSystemAdmin`` instead of ``RequireScopedRole``.
 *
 * Ops that need custom dispatch (``move_device`` — same-site vs
 * cross-site is resolved at request time) sit here with the
 * conservative choice: the highest-privilege branch, so the UI hides
 * the button when the caller can't do the strictest case.
 */

export type ScopeKind = 'device' | 'device_group' | 'site';
export type AssignmentRole = 'observer' | 'operator' | 'admin';

export interface OpDefinition {
  scope_kind: ScopeKind;
  min_role: AssignmentRole;
}

export const OP_MIN_ROLE = {
  // ── Device reads / writes ─────────────────────────────────────────
  read_device:                { scope_kind: 'device' as const,       min_role: 'observer' as const },
  edit_device:                { scope_kind: 'device' as const,       min_role: 'admin' as const },
  delete_device:              { scope_kind: 'device' as const,       min_role: 'admin' as const },
  register_device:            { scope_kind: 'site' as const,         min_role: 'admin' as const },
  // move_device is a synthetic op — the backend dispatches to
  // move_device_same_site vs move_device_cross_site at request time.
  // Both branches are also listed so callers can pick the exact one
  // when they already know the intent (or use ``move_device_cross_site``
  // as the strictest guardrail when the target isn't known yet).
  move_device_same_site:      { scope_kind: 'device' as const,       min_role: 'operator' as const },
  move_device_cross_site:     { scope_kind: 'site' as const,         min_role: 'admin' as const },
  // Retry-rollback on a job's original device. Backend enforces via
  // authorize_device() inline; listed here so useCanPerform has a
  // canonical name to gate the JobDetailModal button on.
  retry_rollback:             { scope_kind: 'device' as const,       min_role: 'operator' as const },
  // ── Group ─────────────────────────────────────────────────────────
  read_group:                 { scope_kind: 'device_group' as const, min_role: 'observer' as const },
  list_group_devices:         { scope_kind: 'device_group' as const, min_role: 'observer' as const },
  create_group:               { scope_kind: 'site' as const,         min_role: 'admin' as const },
  edit_group:                 { scope_kind: 'device_group' as const, min_role: 'admin' as const },
  delete_group:               { scope_kind: 'device_group' as const, min_role: 'admin' as const },
  // ── Site ──────────────────────────────────────────────────────────
  read_site:                  { scope_kind: 'site' as const,         min_role: 'observer' as const },
  edit_site:                  { scope_kind: 'site' as const,         min_role: 'admin' as const },
} as const;

export type Op = keyof typeof OP_MIN_ROLE;

export const ROLE_LEVEL: Record<AssignmentRole, number> = {
  observer: 1,
  operator: 2,
  admin: 3,
};
