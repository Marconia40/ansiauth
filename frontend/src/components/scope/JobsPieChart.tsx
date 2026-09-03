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
  emptyStroke: 'var(--color-panel-border)',
  emptyFill: 'var(--color-panel-elev)',
} as const;

/** Inline-SVG donut. Three slices: success / failed / pending. */
export function JobsPieChart({ data }: { data: JobsPieData }) {
  const total = data.success + data.failed + data.pending;
  const cx = 60;
  const cy = 60;
  const r = 50;
  const strokeW = 20;
  const circumference = 2 * Math.PI * r;

  // With no data, render a hollow ring so the card doesn't look broken.
  if (total === 0) {
    return (
      <div className="flex items-center gap-4">
        <svg width={120} height={120} viewBox="0 0 120 120">
          <circle
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={COLORS.emptyStroke}
            strokeWidth={strokeW}
          />
        </svg>
        <div className="flex flex-col gap-1 text-sm">
          <PieLegendRow color={COLORS.success} label="SUCCESS" value={0} />
          <PieLegendRow color={COLORS.failed} label="FAILED" value={0} />
          <PieLegendRow color={COLORS.pending} label="PENDING" value={0} />
          <RollbackRow value={data.rollbackCount} />
        </div>
      </div>
    );
  }

  const slices = [
    { value: data.success, color: COLORS.success },
    { value: data.failed, color: COLORS.failed },
    { value: data.pending, color: COLORS.pending },
  ];

  let cumulative = 0;
  const segments = slices.map((slice, idx) => {
    if (slice.value === 0) return null;
    const fraction = slice.value / total;
    const dash = fraction * circumference;
    const offset = circumference - cumulative * circumference;
    cumulative += fraction;
    return (
      <circle
        key={idx}
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={slice.color}
        strokeWidth={strokeW}
        strokeDasharray={`${dash} ${circumference - dash}`}
        strokeDashoffset={offset}
        // Start at 12 o'clock (SVG circles start at 3 o'clock by default).
        transform={`rotate(-90 ${cx} ${cy})`}
      />
    );
  });

  return (
    <div className="flex items-center gap-4">
      <div className="relative">
        <svg width={120} height={120} viewBox="0 0 120 120">
          <circle
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={COLORS.emptyFill}
            strokeWidth={strokeW}
          />
          {segments}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
          <span className="text-xl font-semibold text-text leading-none">{total}</span>
          <span className="text-[10px] uppercase tracking-wide text-muted mt-1">Total</span>
        </div>
      </div>
      <div className="flex flex-col gap-1 text-sm">
        <PieLegendRow color={COLORS.success} label="SUCCESS" value={data.success} />
        <PieLegendRow color={COLORS.failed} label="FAILED" value={data.failed} />
        <PieLegendRow color={COLORS.pending} label="PENDING" value={data.pending} />
        <RollbackRow value={data.rollbackCount} />
      </div>
    </div>
  );
}

function PieLegendRow({
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
