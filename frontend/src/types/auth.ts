export type Role = 'observer' | 'operator' | 'admin' | 'super-admin';

export interface AuthUser {
  username: string;
  role: Role;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
}
