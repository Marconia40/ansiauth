/**
 * Matches backend SiteRead post-Phase 4.
 *
 * MSP: `kind` distinguishes REGULAR sites from the single BASE_INFRASTRUCTURE
 * site (D14: hidden from non system-admins). `default_group_id` is
 * guaranteed present at runtime (the app service enforces non-null even
 * though the DB column stays nullable for the cyclic-FK bootstrap dance).
 */
export type SiteKind = 'REGULAR' | 'BASE_INFRASTRUCTURE';

export interface Site {
  id: number;
  name: string;
  description: string | null;
  kind: SiteKind;
  default_group_id: number;
  created_at: string;
  updated_at: string;
  device_count: number;
}

export interface SiteCreate {
  name: string;
  description?: string;
}

export interface SiteUpdate {
  name?: string;
  description?: string;
}
