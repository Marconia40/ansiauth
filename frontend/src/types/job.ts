// Matches the _format_job() output in backend/app/api/jobs.py
export type JobStatus =
  | 'pending'
  | 'running'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface Job {
  job_id: string;
  status: JobStatus;
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
  pre_state: unknown;
  last_error: string | null;
  current_step: string | null;
}

// Statuses that mean the job is still running and should be polled
export const ACTIVE_JOB_STATUSES: JobStatus[] = ['pending', 'running', 'retrying'];
