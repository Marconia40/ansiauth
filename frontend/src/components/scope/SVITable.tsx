'use client';

import type { Scope } from './ScopeDashboard';
import type { SviRow } from './scopeSvis';

interface Props {
  scope: Scope;
  rows: SviRow[];
  isLoading: boolean;
  isError: boolean;
}

/** Tabla de SVIs. Columna "Device" solo cuando el scope agrupa múltiples
 * devices (site/group/org); en device scope se oculta porque sería
 * redundante -- todas las filas serían del mismo device. */
export function SVITable({ scope, rows, isLoading, isError }: Props) {
  const showDeviceColumn = scope.kind !== 'device';

  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading virtual interfaces…</p>;
  }
  if (isError) {
    return <p className="text-sm text-danger py-6">Failed to load SVI data.</p>;
  }
  if (rows.length === 0) {
    return (
      <p className="text-sm italic text-muted py-6">
        No virtual interfaces discovered in this scope yet.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-md border border-panel-border">
      <table className="w-full text-sm">
        <thead className="bg-panel-elev/60 text-muted uppercase text-xs tracking-wider">
          <tr>
            {showDeviceColumn && (
              <th className="text-left font-semibold px-4 py-2">Device</th>
            )}
            <th className="text-left font-semibold px-4 py-2 w-20">VLAN</th>
            <th className="text-left font-semibold px-4 py-2">Description</th>
            <th className="text-left font-semibold px-4 py-2 w-24">State</th>
            <th className="text-left font-semibold px-4 py-2">IPv4</th>
            <th className="text-left font-semibold px-4 py-2">IPv6</th>
            <th className="text-left font-semibold px-4 py-2 w-32">ACL (in / out)</th>
            <th className="text-left font-semibold px-4 py-2 w-24">DHCP Relay</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-panel-border">
          {rows.map((row) => (
            <tr
              key={`${row.device}:${row.vlanId}`}
              className="hover:bg-panel-elev/40 transition-colors"
            >
              {showDeviceColumn && (
                <td className="px-4 py-2 text-text tabular-nums">{row.device}</td>
              )}
              <td className="px-4 py-2 font-semibold text-text tabular-nums">
                {row.vlanId}
              </td>
              <td className="px-4 py-2 text-text">
                {row.description || <span className="text-muted italic">—</span>}
              </td>
              <td className="px-4 py-2">
                <StateBadges adminUp={row.adminUp} operationalUp={row.operationalUp} />
              </td>
              <td className="px-4 py-2 text-text tabular-nums">
                <Ipv4Cell primary={row.ipv4} secondary={row.ipv4Secondary} />
              </td>
              <td className="px-4 py-2 text-text tabular-nums">
                {row.ipv6 || <span className="text-muted italic">—</span>}
              </td>
              <td className="px-4 py-2 text-text">
                <AclCell aclIn={row.aclIn} aclOut={row.aclOut} />
              </td>
              <td className="px-4 py-2 text-text">
                <DhcpRelayCell servers={row.dhcpRelayServers} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Dos chips chicas: admin (Up/Down) y operational (Up/Down). El operator
 * puede tener admin=up pero op=down si el equipo del otro lado está apagado.
 * Ambos ""=null" se renderizan como "—". */
function StateBadges({
  adminUp,
  operationalUp,
}: {
  adminUp: boolean | null;
  operationalUp: boolean | null;
}) {
  return (
    <div className="flex flex-wrap gap-1">
      <StateChip label="A" up={adminUp} title="Admin state" />
      <StateChip label="O" up={operationalUp} title="Operational state" />
    </div>
  );
}

function StateChip({
  label,
  up,
  title,
}: {
  label: string;
  up: boolean | null;
  title: string;
}) {
  const bg =
    up === true
      ? 'bg-success/20 text-success border-success/50'
      : up === false
      ? 'bg-danger/20 text-danger border-danger/50'
      : 'bg-panel-elev text-muted border-panel-border';
  const value = up === true ? 'UP' : up === false ? 'DN' : '—';
  return (
    <span
      className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider border ${bg}`}
      title={`${title}: ${value}`}
    >
      <span className="opacity-60">{label}</span>
      <span>{value}</span>
    </span>
  );
}

function Ipv4Cell({
  primary,
  secondary,
}: {
  primary: string | null;
  secondary: string | null;
}) {
  if (!primary && !secondary) {
    return <span className="text-muted italic">—</span>;
  }
  return (
    <div className="flex flex-col gap-0.5">
      <span>{primary || <span className="text-muted italic">— (no primary)</span>}</span>
      {secondary && (
        <span className="text-xs text-muted">
          <span className="opacity-70">sec:</span> {secondary}
        </span>
      )}
    </div>
  );
}

function AclCell({
  aclIn,
  aclOut,
}: {
  aclIn: string | null;
  aclOut: string | null;
}) {
  if (!aclIn && !aclOut) {
    return <span className="text-muted italic">—</span>;
  }
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <span>
        <span className="opacity-70">in:</span>{' '}
        {aclIn || <span className="text-muted italic">—</span>}
      </span>
      <span>
        <span className="opacity-70">out:</span>{' '}
        {aclOut || <span className="text-muted italic">—</span>}
      </span>
    </div>
  );
}

function DhcpRelayCell({ servers }: { servers: string[] }) {
  if (servers.length === 0) {
    return <span className="text-muted italic">—</span>;
  }
  if (servers.length === 1) {
    return <span className="text-xs tabular-nums">{servers[0]}</span>;
  }
  return (
    <span
      className="text-xs tabular-nums"
      title={servers.join('\n')}
    >
      {servers.length} servers
    </span>
  );
}
