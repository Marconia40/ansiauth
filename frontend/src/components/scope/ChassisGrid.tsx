'use client';

import { useMemo } from 'react';
import type { Port } from '@/types/port';

interface Props {
  ports: Port[];
  selectedName: string | null;
  onSelect: (portName: string) => void;
}

/** Natural sort so port names like Gi1/0/2 come before Gi1/0/10. */
function comparePortNames(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

type PortState = 'up' | 'down' | 'shutdown' | 'unknown';

export function classifyPort(port: Port): PortState {
  if (port.admin_up === false) return 'shutdown';
  if (port.operational_up === true) return 'up';
  if (port.operational_up === false) return 'down';
  return 'unknown';
}

const STATE_CLS: Record<PortState, string> = {
  up: 'bg-success/80 border-success',
  down: 'bg-danger/80 border-danger',
  shutdown: 'bg-panel-elev border-panel-border',
  unknown: 'bg-transparent border-panel-border',
};

export function ChassisGrid({ ports, selectedName, onSelect }: Props) {
  const sorted = useMemo(
    () => [...ports].sort((a, b) => comparePortNames(a.name, b.name)),
    [ports],
  );

  // Two-row layout that mirrors the physical face of a switch. Capped at 28
  // columns so 96-port devices don't stretch the page.
  const cols = Math.min(28, Math.max(4, Math.ceil(sorted.length / 2)));

  return (
    <div
      className="inline-grid gap-1.5 p-3 rounded-md bg-panel-elev/40 border border-panel-border"
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
      role="grid"
      aria-label="Chassis view"
    >
      {sorted.map((port) => {
        const state = classifyPort(port);
        const active = port.name === selectedName;
        return (
          <button
            key={port.name}
            type="button"
            aria-label={`${port.name} — ${state}`}
            aria-pressed={active}
            title={`${port.name} (${state})`}
            onClick={() => onSelect(port.name)}
            className={`h-6 w-6 rounded-sm border-2 transition-transform ${
              STATE_CLS[state]
            } ${
              active
                ? 'ring-2 ring-offset-1 ring-offset-panel'
                : 'hover:scale-110'
            }`}
            style={active ? { borderColor: 'var(--color-focus)' } : undefined}
          />
        );
      })}
    </div>
  );
}
