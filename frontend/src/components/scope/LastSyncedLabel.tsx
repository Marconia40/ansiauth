'use client';

import { formatSyncRelative } from './formatSyncRelative';

export interface LastSyncedLabelProps {
  /** ISO timestamp of the last successful sync. */
  syncedAt?: string | null;
  /** If the last sync errored, message shown in danger tone. */
  syncError?: string | null;
  /** While a scope refresh runs, e.g. "3 in progress". Replaces the
   * synced-at label until the refresh finishes. */
  progressLabel?: string | null;
}

/** Passive indicator of the last successful sync — used at scope level
 * (site / device-group / org). Replaces the old manual RefreshButton at
 * that level: dashboards now refresh automatically via Celery Beat +
 * silent on-open, so the button became misleading. RefreshButton itself
 * stays in use at device level (DeviceDashboard) where per-device manual
 * refresh is still exposed. */
export function LastSyncedLabel({
  syncedAt,
  syncError,
  progressLabel,
}: LastSyncedLabelProps) {
  if (progressLabel) {
    return <span className="text-xs text-muted tabular-nums">{progressLabel}</span>;
  }
  if (syncError) {
    return <span className="text-xs text-danger">{syncError}</span>;
  }
  if (syncedAt) {
    return (
      <span className="text-xs text-muted">
        Last synced {formatSyncRelative(syncedAt)}
      </span>
    );
  }
  return null;
}
