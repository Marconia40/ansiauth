'use client';

import type { Port } from '@/types/port';
import { PortDetailCard } from './PortDetailCard';
import type { PortSelection } from './usePortSelection';

interface Props {
  selection: PortSelection;
  /** Indexed lookup: device → its Port objects — feeds the detail cards. */
  portsByDevice: Map<string, Port[]>;
}

export function PortSelectionSummary({ selection, portsByDevice }: Props) {
  return (
    <div className="flex flex-col gap-3">
      <header className="flex items-center justify-between">
        <div className="text-sm text-text">
          <span className="font-semibold tabular-nums">{selection.count}</span>{' '}
          port{selection.count === 1 ? '' : 's'} selected across{' '}
          <span className="font-semibold tabular-nums">{selection.deviceCount}</span>{' '}
          device{selection.deviceCount === 1 ? '' : 's'}
        </div>
        <button
          type="button"
          onClick={selection.clearAll}
          disabled={selection.count === 0}
          className="text-xs uppercase tracking-wider border border-panel-border rounded px-3 py-1 hover:bg-panel-elev disabled:opacity-40"
        >
          Clear
        </button>
      </header>

      {selection.count === 0 ? (
        <p className="text-sm italic text-muted">
          Click ports on any chassis above to build a selection.
        </p>
      ) : (
        <>
          <div className="rounded-md border border-panel-border divide-y divide-panel-border">
            {Array.from(selection.byDevice.entries()).map(([device, ifaces]) => (
              <div key={device} className="px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-semibold text-text truncate">{device}</span>
                  <button
                    type="button"
                    onClick={() => selection.clearDevice(device)}
                    className="text-xs text-muted hover:text-danger transition"
                  >
                    clear ({ifaces.length})
                  </button>
                </div>
                <div className="text-xs text-muted break-words mt-1 tabular-nums">
                  {ifaces.join(', ')}
                </div>
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-3">
            {Array.from(selection.byDevice.entries()).flatMap(([device, ifaces]) => {
              const ports = portsByDevice.get(device) ?? [];
              return ifaces.map((iface) => {
                const port = ports.find((p) => p.name === iface) ?? null;
                const isLast =
                  selection.lastClicked?.device === device &&
                  selection.lastClicked.interface === iface;
                return (
                  <PortDetailBlock
                    key={`${device}::${iface}`}
                    device={device}
                    iface={iface}
                    port={port}
                    highlight={isLast}
                  />
                );
              });
            })}
          </div>
        </>
      )}
    </div>
  );
}

function PortDetailBlock({
  device,
  iface,
  port,
  highlight,
}: {
  device: string;
  iface: string;
  port: Port | null;
  highlight: boolean;
}) {
  return (
    <div>
      <div
        className={`text-xs uppercase tracking-wider mb-1 ${
          highlight ? 'text-info font-semibold' : 'text-muted'
        }`}
      >
        {device} &gt; {iface}
        {highlight && <span className="ml-2 normal-case">· last clicked</span>}
      </div>
      <PortDetailCard
        port={port}
        emptyLabel="Port details unavailable — try refreshing."
      />
    </div>
  );
}
