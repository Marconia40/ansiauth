import type { Role } from './auth';

/**
 * MSP role kept for the RoleAssignment body — `observer`/`operator`/`admin`
 * are per-scope roles; system-admin lives on the user row itself
 * (`is_system_admin`).
 */
export type AssignmentRole = 'observer' | 'operator' | 'admin';

/**
 * A grant of a role on either a whole site (device_group_id === null) or a
 * specific group within a site (device_group_id !== null). Denormalized
 * `site_name` / `device_group_name` / `created_by_username` are populated
 * by the backend so frontends can render without follow-up calls.
 */
export interface RoleAssignment {
  id: number;
  user_id: number;
  site_id: number;
  site_name: string | null;
  device_group_id: number | null;
  device_group_name: string | null;
  role: AssignmentRole;
  created_at: string;
  created_by_user_id: number | null;
  created_by_username: string | null;
}

/**
 * Body for `POST /users/{user_id}/grants`.
 */
export interface RoleAssignmentCreate {
  site_id: number;
  device_group_id?: number | null;
  role: AssignmentRole;
}

/**
 * Body for `PUT /users/{user_id}/system-admin`.
 */
export interface SystemAdminUpdate {
  is_system_admin: boolean;
}

/**
 * Matches backend UserRead post-Phase 5.
 *
 * ``is_system_admin`` is the sole system-wide privilege bit; per-scope
 * permissions live in ``role_assignments`` (fetched via ``listGrants``).
 * The legacy ``role`` field is kept optional so components that display it
 * for the currently-logged-in user (synthesized from ``is_system_admin``)
 * still type-check — it is never populated by the backend for other users.
 */
export interface User {
  id: number;
  username: string;
  email: string | null;
  is_active: boolean;
  is_system_admin: boolean;
  created_at: string;
  updated_at: string;
  role?: Role;
}

/**
 * Matches backend UserCreate. To grant per-scope roles, create the user then
 * issue `POST /users/{id}/grants`.
 */
export interface UserCreate {
  username: string;
  password: string;
  email?: string;
  is_system_admin?: boolean;
}

/**
 * Matches backend UserUpdate — only mutable profile fields. Toggling
 * ``is_system_admin`` has its own dedicated endpoint.
 */
export interface UserUpdate {
  email?: string;
  is_active?: boolean;
  password?: string;
}
