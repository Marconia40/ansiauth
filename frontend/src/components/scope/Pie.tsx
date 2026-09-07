export interface PieSlice {
  value: number;
  /** CSS color — hex, rgb, or var() expression. */
  color: string;
}

interface Props {
  slices: PieSlice[];
  /** Outer diameter in px. Default 120. */
  size?: number;
  /** Donut ring thickness in px. Default 20. */
  strokeWidth?: number;
  /** Optional centered content — usually a big total. */
  center?: React.ReactNode;
  /** Fallback ring color when total = 0. */
  emptyColor?: string;
  /** Background ring behind the slices (visible when slices don't fill 100%). */
  trackColor?: string;
}

/**
 * Reusable donut chart built with a single SVG ring per slice using
 * strokeDasharray offsets. No external chart lib. Slices with value=0 are
 * skipped; when the total is 0 a hollow ring is drawn so the card doesn't
 * collapse.
 */
export function Pie({
  slices,
  size = 120,
  strokeWidth = 20,
  center,
  emptyColor = 'var(--color-panel-border)',
  trackColor = 'var(--color-panel-elev)',
}: Props) {
  const cx = size / 2;
  const cy = size / 2;
  const r = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * r;
  const total = slices.reduce((acc, s) => acc + Math.max(0, s.value), 0);

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke={total === 0 ? emptyColor : trackColor}
          strokeWidth={strokeWidth}
        />
        {total > 0 && (() => {
          let cumulative = 0;
          return slices.map((slice, idx) => {
            if (slice.value <= 0) return null;
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
                strokeWidth={strokeWidth}
                strokeDasharray={`${dash} ${circumference - dash}`}
                strokeDashoffset={offset}
                // Start at 12 o'clock (SVG circles begin at 3 o'clock by default).
                transform={`rotate(-90 ${cx} ${cy})`}
              />
            );
          });
        })()}
      </svg>
      {center && (
        <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none text-center">
          {center}
        </div>
      )}
    </div>
  );
}
