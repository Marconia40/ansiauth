/**
 * Shared polling interval for cache-first ``synced`` endpoints while a
 * sync is in progress (``sync_in_progress: true`` in the envelope). Every
 * ``refetchInterval`` in the app that watches ``sync_in_progress`` /
 * ``sync_in_progress_count`` should reference this constant instead of a
 * hardcoded number -- one place to tune it.
 *
 * Decision 4 of docs/SSH_REFRESH_PLAN.md: bumped from 2s to 8s. Reasons:
 * a sync playbook takes 3-30+ seconds, so 2s was firing many redundant
 * fetches per sync (each landing DB queries and re-renders). 8s stays
 * responsive enough that the user sees updates without waiting, but
 * cuts polling load ~4x. Combined with React Query's default
 * ``refetchIntervalInBackground: false``, background tabs stop polling
 * entirely -- no wasted refetches while nobody's looking.
 */
export const SYNC_POLL_INTERVAL_MS = 8000;
