'use client';

import { useMemo } from 'react';
import type { Port } from '@/types/port';
import { classifyPort } from './ChassisGrid';
import type { PortRef, PortSelection } from './usePortSelection';

interface Props {
  device: string;
  ports: Port[];
  selection: PortSelection;
}

/** Natural sort so port names like Gi1/0/2 come before Gi1/0/10. */
function comparePortNames(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

const STATE_CLS = {
  up: 'bg-success/80 border-success',
  down: 'bg-danger/80 border-danger',
  shutdown: 'bg-panel-elev border-panel-border',
  unknown: 'bg-transparent border-panel-border',
} as const;

/**
 * Same visual language as ChassisGrid but with multi-select: clicking toggles
 * a port in/out of the shared selection state and updates `lastClicked` so
 * the detail panel underneath the summary card refreshes.
 */
export function ChassisMulti({ device, ports, selection }: Props) {
  const sorted = useMemo(
    () => [...ports].sort((a, b) => comparePortNames(a.name, b.name)),
    [ports],
  );
  const cols = Math.min(28, Math.max(4, Math.ceil(sorted.length / 2)));

  return (
    <div
      className="inline-grid gap-1.5 p-3 rounded-md bg-panel-elev/40 border border-panel-border"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
    >
      {sorted.map((port) => {
        const state = classifyPort(port);
        const ref: PortRef = { device, interface: port.name };
        const active = selection.isSelected(ref);
        const isLast =
          selection.lastClicked?.device === device &&
          selection.lastClicked.interface === port.name;
        return (
          <button
            key={port.name}
            type="button"
            aria-label={`${port.name} — ${state}`}
            aria-pressed={active}
            title={`${device} · ${port.name} (${state})`}
            onClick={() => selection.toggle(ref)}
            className={`h-6 w-6 rounded-sm border-2 transition-transform ${
              STATE_CLS[state]
            } ${active ? 'ring-2 ring-offset-1 ring-offset-panel' : 'hover:scale-110'}`}
            style={
              active
                ? {
                    borderColor: 'var(--color-focus)',
                    boxShadow: isLast
                      ? '0 0 0 2px var(--color-info) inset'
                      : undefined,
                  }
                : undefined
            }
          />
        );
      })}
    </div>
  );
}
