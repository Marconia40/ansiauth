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
 * Matches backend UserRead post-Phase 4.
 *
 * MSP: `is_system_admin` replaces the legacy super-admin/admin distinction
 * for globally-privileged operations. Per-scope permissions live in
 * `grants` (populated by `listGrants` — not carried on this base shape to
 * keep the users list light).
 */
export interface User {
  id: number;
  username: string;
  email: string | null;
  role: Role;
  is_active: boolean;
  is_system_admin?: boolean;
  created_at: string;
  updated_at: string;
  /** @deprecated Phase 4 — replaced by `grants`. Kept for one release. */
  allowed_site_ids?: number[];
  /** @deprecated Phase 4 — replaced by `grants`. Kept for one release. */
  allowed_site_names?: string[];
}

/**
 * Matches backend UserCreate.
 *
 * The legacy `allowed_site_ids` shape is retained through Phase 4 and
 * removed in Phase 5; new callers should create the user, then issue
 * `POST /users/{id}/grants` per desired scope.
 */
export interface UserCreate {
  username: string;
  password: string;
  role: Role;
  email?: string;
  /** @deprecated Phase 4 — prefer post-create `grant` calls. */
  allowed_site_ids?: number[];
}

/**
 * Matches backend UserUpdate.
 */
export interface UserUpdate {
  email?: string;
  role?: Role;
  is_active?: boolean;
  password?: string;
  /** @deprecated Phase 4 — replaced by the grants endpoints. */
  allowed_site_ids?: number[];
}
