export type Role = 'observer' | 'operator' | 'admin' | 'super-admin';

export interface AuthUser {
  /** Users row PK. Carried in the JWT ``id`` claim so per-scope gates can
   * fetch ``listGrants(user.id)`` without a /users/me round-trip. Absent
   * for pre-Phase-5 tokens that hadn't seen the claim yet — treat
   * ``undefined`` as "grants unknown, degrade to observer for the UI
   * gate" (see useMyGrants). */
  id?: number;
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
