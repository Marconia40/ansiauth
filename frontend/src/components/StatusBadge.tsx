type StatusDef = { cls: string; label: string };

const STATUS_MAP: Record<string, StatusDef> = {
  completed:          { cls: 'bg-green-100 text-green-700',   label: 'Completed' },
  failed:             { cls: 'bg-red-100 text-red-700',       label: 'Failed' },
  cancelled:          { cls: 'bg-red-100 text-red-700',       label: 'Cancelled' },
  running:            { cls: 'bg-amber-100 text-amber-700',   label: 'Running...' },
  retrying:           { cls: 'bg-amber-100 text-amber-700',   label: 'Retrying...' },
  pending:            { cls: 'bg-panel-elev text-muted',      label: 'Queued' },
  queued:             { cls: 'bg-panel-elev text-muted',      label: 'Queued' },
  partial_success:    { cls: 'bg-orange-100 text-orange-700', label: 'Partial failure' },
  partial_failure:    { cls: 'bg-orange-100 text-orange-700', label: 'Partial failure' },
  rollback_performed: { cls: 'bg-orange-100 text-orange-700', label: 'Rollback executed' },
};

export function StatusBadge({ status }: { status: string }) {
  const def = STATUS_MAP[status] ?? { cls: 'bg-panel-elev text-muted', label: status.replace(/_/g, ' ') };
  const isActive = status === 'running' || status === 'retrying';
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${def.cls}`}>
      {isActive && (
        <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse flex-shrink-0" />
      )}
      {def.label}
    </span>
  );
}
