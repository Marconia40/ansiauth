import type { Role } from './auth';

// Matches backend UserRead schema
export interface User {
  id: number;
  username: string;
  email: string | null;
  role: Role;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  allowed_site_ids: number[];
  allowed_site_names: string[];
}

// Matches backend UserCreate schema
export interface UserCreate {
  username: string;
  password: string;
  role: Role;
  email?: string;
  allowed_site_ids?: number[];
}

// Matches backend UserUpdate schema. Omitting `allowed_site_ids` leaves the
// existing assignment intact; passing `[]` explicitly clears it.
export interface UserUpdate {
  email?: string;
  role?: Role;
  is_active?: boolean;
  password?: string;
  allowed_site_ids?: number[];
}
