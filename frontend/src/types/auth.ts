export type Role = 'observer' | 'operator' | 'admin' | 'super-admin';

export interface AuthUser {
  username: string;
  /** Synthesized post-Phase-5: `admin` when the caller is a system-admin,
   * `observer` otherwise. Per-scope permissions live in role_assignments. */
  role: Role;
  is_system_admin: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}
