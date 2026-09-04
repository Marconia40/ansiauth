'use client';

import type { Scope } from './ScopeDashboard';
import type { VlanRow } from './scopeVlans';

interface Props {
  scope: Scope;
  rows: VlanRow[];
  isLoading: boolean;
  isError: boolean;
}

/** Renders the "Available VLANs" table. Columns depend on scope. */
export function VlanTable({ scope, rows, isLoading, isError }: Props) {
  const showPortsColumn = scope.kind === 'device';

  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading VLANs…</p>;
  }
  if (isError) {
    return <p className="text-sm text-danger py-6">Failed to load VLAN data.</p>;
  }
  if (rows.length === 0) {
    return (
      <p className="text-sm italic text-muted py-6">
        No VLANs discovered in this scope yet.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-md border border-panel-border">
      <table className="w-full text-sm">
        <thead className="bg-panel-elev/60 text-muted uppercase text-xs tracking-wider">
          <tr>
            <th className="text-left font-semibold px-4 py-2 w-20">ID</th>
            <th className="text-left font-semibold px-4 py-2">
              {showPortsColumn ? 'Ports' : 'Devices'}
            </th>
            <th className="text-left font-semibold px-4 py-2 w-48">Description</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-panel-border">
          {rows.map((row) => (
            <tr
              key={row.vlanId}
              className="hover:bg-panel-elev/40 transition-colors"
            >
              <td className="px-4 py-2 font-semibold text-text tabular-nums">
                {row.vlanId}
                {row.nameDiscrepancy && (
                  <span
                    className="text-warning font-bold ml-1"
                    title={`Name differs across devices: ${row.names.join(', ')}`}
                  >
                    *
                  </span>
                )}
              </td>
              <td className="px-4 py-2 text-text">
                {showPortsColumn ? (
                  <PortsList items={row.ports} />
                ) : (
                  <DevicesList items={row.devices} />
                )}
              </td>
              <td className="px-4 py-2 text-text">
                {row.name || <span className="text-muted italic">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PortsList({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <span className="italic text-muted">— no ports assigned —</span>;
  }
  return <span className="tabular-nums">{items.join(' , ')}</span>;
}

function DevicesList({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <span className="italic text-muted">—</span>;
  }
  return <span>{items.join(' , ')}</span>;
}
