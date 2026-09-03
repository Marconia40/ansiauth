import { Pie } from './Pie';

export interface JobsPieData {
  success: number;
  failed: number;
  pending: number;
  rollbackCount: number;
}

const COLORS = {
  success: 'var(--color-accent-success)',
  failed: 'var(--color-accent-danger)',
  pending: 'var(--color-accent-warning)',
} as const;

/** Donut for job outcomes with a legend and a rollback footer. */
export function JobsPieChart({ data }: { data: JobsPieData }) {
  const total = data.success + data.failed + data.pending;

  return (
    <div className="flex items-center gap-4">
      <Pie
        size={120}
        strokeWidth={20}
        slices={[
          { value: data.success, color: COLORS.success },
          { value: data.failed, color: COLORS.failed },
          { value: data.pending, color: COLORS.pending },
        ]}
        center={
          <>
            <span className="text-xl font-semibold text-text leading-none">
              {total}
            </span>
            <span className="text-[10px] uppercase tracking-wide text-muted mt-1">
              Total
            </span>
          </>
        }
      />
      <div className="flex flex-col gap-1 text-sm">
        <LegendRow color={COLORS.success} label="SUCCESS" value={data.success} />
        <LegendRow color={COLORS.failed} label="FAILED" value={data.failed} />
        <LegendRow color={COLORS.pending} label="PENDING" value={data.pending} />
        <RollbackRow value={data.rollbackCount} />
      </div>
    </div>
  );
}

function LegendRow({
  color,
  label,
  value,
}: {
  color: string;
  label: string;
  value: number;
}) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="inline-block h-3 w-3 rounded-sm"
        style={{ background: color }}
        aria-hidden
      />
      <span className="text-xs font-semibold uppercase tracking-wider text-muted w-20">
        {label}
      </span>
      <span className="text-sm font-semibold text-text tabular-nums">{value}</span>
    </div>
  );
}

function RollbackRow({ value }: { value: number }) {
  return (
    <div className="flex items-center gap-2 mt-1 pt-1 border-t border-panel-border">
      <span className="text-xs uppercase tracking-wider text-muted w-24">
        Rollbacks
      </span>
      <span className="text-sm font-semibold text-text tabular-nums">{value}</span>
    </div>
  );
}
