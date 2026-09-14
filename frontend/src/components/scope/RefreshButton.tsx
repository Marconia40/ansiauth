'use client';

import { RefreshIcon } from '../Icon';
import { formatSyncRelative } from './formatSyncRelative';

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
        // Bug real reportado por el usuario: este label solo decia "Sync
        // error" sin ninguna forma de ver el detalle real -- a diferencia
        // de las tabs de Global Config (ver GlobalConfigArpMac.tsx y
        // hermanas), que muestran un banner completo con el mensaje. Los
        // callers de RefreshButton (Ports/VLAN/Virtual-Interfaces) nunca
        // tuvieron ese banner -- el tooltip nativo del navegador es el fix
        // minimo que cubre los 3 de una sola vez sin agregar un banner
        // nuevo en cada tab.
        <span className="text-xs text-danger cursor-help" title={syncError}>
          Sync error
        </span>
      ) : syncedAt ? (
        <span className="text-xs text-muted">
          Last synced {formatSyncRelative(syncedAt)}
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
