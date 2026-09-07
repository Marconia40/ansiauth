'use client';

import { RefreshIcon } from '../Icon';

export interface RefreshButtonProps {
  onClick?: () => void;
  loading?: boolean;
  disabled?: boolean;
  /** Optional "Last synced 3m ago" label rendered next to the button. */
  syncedAt?: string | null;
  /** If the last sync errored, message shown in danger tone. */
  syncError?: string | null;
  /** While a multi-device refresh runs, e.g. "12 / 20 synced". Replaces the
   * synced-at label until the refresh finishes. */
  progressLabel?: string | null;
}

function formatRelative(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diffMs = Date.now() - then;
  if (diffMs < 45_000) return 'just now';
  const mins = Math.round(diffMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export function RefreshButton({
  onClick,
  loading,
  disabled,
  syncedAt,
  syncError,
  progressLabel,
}: RefreshButtonProps) {
  return (
    <div className="flex items-center gap-2">
      {progressLabel ? (
        <span className="text-xs text-muted tabular-nums">{progressLabel}</span>
      ) : syncError ? (
        <span className="text-xs text-danger">Sync error</span>
      ) : syncedAt ? (
        <span className="text-xs text-muted">
          Last synced {formatRelative(syncedAt)}
        </span>
      ) : null}
      <button
        type="button"
        onClick={onClick}
        disabled={disabled || loading}
        className="inline-flex items-center gap-2 rounded-md border border-panel-border bg-panel px-3 py-1.5 text-xs font-semibold uppercase tracking-wider text-text hover:bg-panel-elev disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
      >
        <RefreshIcon size={14} className={loading ? 'animate-spin' : undefined} />
        {loading ? 'Refreshing…' : 'Refresh'}
      </button>
    </div>
  );
}
