import type { Port } from '@/types/port';

interface Props {
  port: Port | null;
  /** Optional hint when no port is selected. */
  emptyLabel?: string;
}

/** Formats the "TYPE" line — mirrors the vendor terminology in the mock. */
function typeLabel(port: Port): string {
  if (port.mode === 'access') return 'ACCESS';
  if (port.mode === 'trunk') return 'TRUNK';
  return '—';
}

// Backend no longer reads PoE / speed / duplex from devices (removed in
// 49ec202 because the collectors never populated them). Keeping the rows
// with a hardcoded 'N/A' for now; drop them if the layout looks off.
function poeLabel(_port: Port): string {
  return 'N/A';
}

function shutdownLabel(port: Port): string {
  if (port.admin_up === true) return 'NO';
  if (port.admin_up === false) return 'YES';
  return '—';
}

/** Access ports show only their access VLAN, trunks show the PVID (also
 * carried in access_vlan by the backend collector). */
function vlanLabel(port: Port): string {
  if (port.access_vlan === null || port.access_vlan === undefined) return '—';
  return String(port.access_vlan);
}

function allowedLabel(port: Port): string {
  if (port.mode !== 'trunk') return '-N/A-';
  const list = port.allowed_vlans ?? [];
  if (list.length === 0) return '—';
  return list.join(', ');
}

function speedLabel(_port: Port): string {
  return '—';
}

/** OFF cuando el device confirmó que no hay storm-control; el porcentaje
 * cuando lo hay; ON sin % si el device lo tiene configurado en pps/bps
 * (nuestro write path solo produce percent, esto solo pasa con configs
 * preexistentes); "—" si el parser no matcheo la salida (no inventar). */
function stormControlLabel(port: Port): string {
  if (port.storm_control_enabled === false) return 'OFF';
  if (port.storm_control_enabled === true) {
    let base: string;
    if (port.storm_control_threshold === null || port.storm_control_threshold === undefined) {
      base = 'ON';
    } else {
      // Redondear los decimales cuando son .00 para mostrar "10%" en vez de
      // "10.00%" -- match visual con lo que muestra Cisco al operador.
      const t = port.storm_control_threshold;
      const shown = Number.isInteger(t) ? String(t) : t.toFixed(2);
      base = `${shown}%`;
    }
    const extras: string[] = [];
    if (port.storm_control_action) extras.push(port.storm_control_action);
    if (port.storm_control_trap) extras.push('trap');
    return extras.length > 0 ? `${base} · ${extras.join(' · ')}` : base;
  }
  return '—';
}

export function PortDetailCard({ port, emptyLabel }: Props) {
  if (!port) {
    return (
      <div className="rounded-md bg-panel-elev/60 border border-panel-border p-4 text-sm text-muted italic">
        {emptyLabel ?? 'Click a port on the chassis to see its configuration.'}
      </div>
    );
  }

  return (
    <div className="rounded-md bg-panel-elev/60 border border-panel-border p-4">
      <div className="flex items-baseline gap-2 mb-3">
        <span className="text-lg font-bold text-text tracking-wide">
          {port.name.toUpperCase()}:
        </span>
      </div>
      <ul className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
        <DetailRow label="DESCRIPTION" value={port.description || '—'} />
        <DetailRow label="TYPE" value={typeLabel(port)} />
        <DetailRow label="NATIVE VLAN" value={vlanLabel(port)} />
        <DetailRow label="POE" value={poeLabel(port)} />
        <DetailRow label="SHUTDOWN" value={shutdownLabel(port)} />
        <DetailRow label="UPLINK" value={speedLabel(port)} />
        <DetailRow
          label="ALLOWED VLANS"
          value={allowedLabel(port)}
          className="md:col-span-2"
        />
        <DetailRow
          label="STORM CONTROL"
          value={stormControlLabel(port)}
          className="md:col-span-2"
        />
      </ul>
    </div>
  );
}

function DetailRow({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <li className={`flex items-start gap-2 ${className ?? ''}`}>
      <span className="text-info leading-6" aria-hidden>
        •
      </span>
      <span className="font-semibold text-muted uppercase tracking-wider text-xs mt-0.5 w-32 shrink-0">
        {label} =
      </span>
      <span className="text-sm text-text break-words">{value}</span>
    </li>
  );
}
