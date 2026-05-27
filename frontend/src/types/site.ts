export interface Site {
  id: number;
  name: string;
  description: string | null;
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
