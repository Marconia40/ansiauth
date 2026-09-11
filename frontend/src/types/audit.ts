// Matches backend AuditLogModel fields
export interface AuditLog {
  id: number;
  timestamp: string;
  user: string;
  action: string;
  resource: string;
  resource_id: string | null;
  status: string;
  details: Record<string, unknown>;
  summary: string | null;
  job_id: string | null;
  device: string | null;
  request_id: string | null;
  parent_audit_id: number | null;
}
