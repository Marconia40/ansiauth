'use client';

import type { Port } from '@/types/port';
import type {
  AccessVlanEditor,
  AdminToggler,
  DescriptionEditor,
  EditorsBusy,
  ModeEditor,
  TrunkVlansEditor,
} from '../types';
import { AdminToggleCell } from './AdminToggleCell';
import { DescriptionCell } from './DescriptionCell';
import { OperBadge } from './StatusBadges';
import { PortModeSelector } from './PortModeSelector';
import { TrunkVlansCell } from './TrunkVlansCell';
import { VlanConfigForm } from './VlanConfigForm';

export interface PortRowProps {
  port: Port;
  canEdit: boolean;
  selected: boolean;
  onToggleSelect: (port: Port, checked: boolean) => void;
  busy: EditorsBusy;
  description: DescriptionEditor;
  admin: AdminToggler;
  mode: ModeEditor;
  accessVlan: AccessVlanEditor;
  trunkVlans: TrunkVlansEditor;
}

export function PortRow({
  port,
  canEdit,
  selected,
  onToggleSelect,
  busy,
  description,
  admin,
  mode,
  accessVlan,
  trunkVlans,
}: PortRowProps) {
  const rowClass = selected
    ? 'bg-blue-50 hover:bg-blue-100'
    : port.admin_up === false
    ? 'bg-gray-50 hover:bg-gray-100'
    : 'hover:bg-gray-50';

  return (
    <tr className={`border-b border-gray-100 transition-colors ${rowClass}`}>
      <td className="px-3 py-2 w-8">
        {canEdit && (
          <input
            type="checkbox"
            checked={selected}
            onChange={(e) => onToggleSelect(port, e.target.checked)}
            disabled={busy.bulkExecuting}
            className="w-4 h-4 accent-blue-600 cursor-pointer disabled:cursor-not-allowed"
            aria-label={`Select ${port.name}`}
          />
        )}
      </td>
      <td className={`px-3 py-2 font-mono text-xs whitespace-nowrap ${port.admin_up === false ? 'text-gray-400' : 'text-gray-900'}`}>
        {port.name}
      </td>
      <td className="px-3 py-2 text-gray-900 max-w-xs">
        <DescriptionCell port={port} canEdit={canEdit} busy={busy} editor={description} />
      </td>
      <td className="px-3 py-2">
        <AdminToggleCell port={port} canEdit={canEdit} bulkExecuting={busy.bulkExecuting} toggler={admin} />
      </td>
      <td className="px-3 py-2"><OperBadge value={port.operational_up} /></td>
      <td className="px-3 py-2">
        <PortModeSelector port={port} canEdit={canEdit} busy={busy} editor={mode} />
      </td>
      <td className="px-3 py-2 text-gray-900">
        <VlanConfigForm port={port} canEdit={canEdit} busy={busy} editor={accessVlan} />
      </td>
      <td className="px-3 py-2 text-gray-900 max-w-xs">
        <TrunkVlansCell port={port} canEdit={canEdit} busy={busy} editor={trunkVlans} />
      </td>
      <td className="px-3 py-2 text-gray-600">
        {port.poe_enabled === null ? 'N/A' : port.poe_enabled ? 'On' : 'Off'}
      </td>
      <td className="px-3 py-2 text-gray-600">{port.speed ?? 'N/A'}</td>
      <td className="px-3 py-2 text-gray-600">{port.duplex ?? 'N/A'}</td>
    </tr>
  );
}
