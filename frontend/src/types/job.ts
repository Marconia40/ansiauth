// Matches the _format_job() output in backend/app/api/jobs.py
export type JobStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'rollback_performed';

export interface JobExecutionSummary {
  attempts: number | null;
  rollback_performed: boolean;
  rollback_success: boolean | null;
  duration_ms: number | null;
}

export interface Job {
  job_id: string;
  status: JobStatus;
  operation: string | null;
  parameters_summary: string | null;
  playbook: string | null;
  device: string | null;
  result: unknown;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  retry_count: number;
  max_retries: number;
  rollback_performed: boolean;
  rollback_success: boolean | null;
  pre_state: unknown;
  last_error: string | null;
  current_step: string | null;
  group_job_id: string | null;
  execution_summary: JobExecutionSummary | null;
}

// Statuses that mean the job is still running and should be polled
export const ACTIVE_JOB_STATUSES: JobStatus[] = ['pending', 'queued', 'running', 'retrying'];

export type GroupJobStatus = 'pending' | 'running' | 'completed' | 'partial_success' | 'partial_failure' | 'failed';

export interface GroupJobDeviceResult {
  device: string;
  job_id: string | null;
  status: string;
  current_step: string | null;
  retry_count: number;
  rollback_performed: boolean;
  rollback_success: boolean | null;
  error: string | null;
  duration_ms: number | null;
}

export interface GroupJobExecutionSummary {
  total_devices: number;
  completed: number;
  failed: number;
  partial_success: boolean;
  rollback_count: number;
  duration_ms: number | null;
}

export interface GroupJob {
  group_job_id: string;
  status: GroupJobStatus;
  operation: string | null;
  playbook: string | null;
  parameters: Record<string, unknown> | null;
  parameters_summary: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  execution_summary: GroupJobExecutionSummary;
  device_results: GroupJobDeviceResult[];
}
