export type Role = 'observer' | 'operator' | 'admin' | 'super-admin';

export interface AuthUser {
  username: string;
  role: Role;
}

// Matches backend TokenResponse schema
export interface TokenResponse {
  access_token: string;
  token_type: string;
  refresh_token: string;
}
