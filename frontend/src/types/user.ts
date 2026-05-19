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
}

// Matches backend UserCreate schema
export interface UserCreate {
  username: string;
  password: string;
  role: Role;
  email?: string;
}

// Matches backend UserUpdate schema
export interface UserUpdate {
  email?: string;
  role?: Role;
  is_active?: boolean;
  password?: string;
}
