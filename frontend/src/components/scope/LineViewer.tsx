'use client';

import { useMemo } from 'react';

interface Props {
  /** Full set of lines (post-fetch). `null` = not synced yet; `[]` = synced empty. */
  lines: string[] | null;
  /** Case-insensitive substring filter applied on the client. */
  filter: string;
  /** Wrap long lines instead of scrolling horizontally. */
  wrap: boolean;
  /** Show numeric prefix (1-based). Useful for running-config; noise for
   *  logs, whose lines usually already carry timestamps. */
  showLineNumbers?: boolean;
  /** Copy shown in place of the viewer when `lines === null`. Sub-tab-
   *  specific so the user knows which endpoint to (re)fresh. */
  neverSyncedLabel: string;
  /** Copy shown when `lines === []` (synced but empty). */
  emptyLabel: string;
  /** Reserve a fixed height so filtering doesn't jump the surrounding
   *  layout around. */
  maxHeightClass?: string;
}

// Shared monospace viewer for the Logs and Running-config sub-tabs.
// Renders a scrollable box with the current lines (optionally filtered
// by a case-insensitive substring). Pure -- no data fetching, no
// refresh action; the parent sub-tab owns those.
export function LineViewer({
  lines,
  filter,
  wrap,
  showLineNumbers,
  neverSyncedLabel,
  emptyLabel,
  maxHeightClass = 'max-h-[60vh]',
}: Props) {
  const filtered = useMemo(() => {
    if (lines === null) return null;
    const q = filter.trim().toLowerCase();
    if (q === '') return lines.map((line, i) => ({ line, index: i }));
    return lines
      .map((line, i) => ({ line, index: i }))
      .filter(({ line }) => line.toLowerCase().includes(q));
  }, [lines, filter]);

  if (lines === null) {
    return (
      <div className="rounded-md border border-panel-border bg-panel-elev/40 text-muted px-3 py-2 text-sm italic">
        {neverSyncedLabel}
      </div>
    );
  }
  if (lines.length === 0) {
    return (
      <p className="text-sm italic text-muted">{emptyLabel}</p>
    );
  }
  if (filtered !== null && filtered.length === 0) {
    return (
      <p className="text-sm italic text-muted">No lines match the filter.</p>
    );
  }
  return (
    <div
      className={`${maxHeightClass} overflow-auto rounded-md bg-panel-elev/60 border border-panel-border`}
    >
      <pre
        className={`text-xs font-mono leading-relaxed py-2 pr-3 ${
          wrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'
        }`}
      >
        {(filtered ?? []).map(({ line, index }) => (
          <div key={index} className="flex">
            {showLineNumbers && (
              <span
                aria-hidden
                className="select-none text-muted/60 tabular-nums w-12 pr-2 text-right shrink-0"
              >
                {index + 1}
              </span>
            )}
            <span className="text-text flex-1 pl-2">{line || ' '}</span>
          </div>
        ))}
      </pre>
    </div>
  );
}
