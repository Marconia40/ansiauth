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

/** POE flag is nullable when the device didn't report the capability. */
function poeLabel(port: Port): string {
  if (port.poe_enabled === true) return 'ENABLE';
  if (port.poe_enabled === false) return 'DISABLE';
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

function speedLabel(port: Port): string {
  return port.speed ?? '—';
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
