'use client';

import type { Port } from '@/types/port';
import type {
  AccessVlanEditor,
  AdminToggler,
  DescriptionEditor,
  EditorsBusy,
  ModeEditor,
  SortDir,
  TrunkVlansEditor,
} from '../types';
import { PortRow } from './PortRow';

export interface PortTableProps {
  pageItems: Port[];
  canEdit: boolean;
  selectedPorts: Set<string>;
  onToggleSelect: (port: Port, checked: boolean) => void;
  onSelectAllVisible: (checked: boolean) => void;
  allPageSelected: boolean;
  somePageSelected: boolean;
  busy: EditorsBusy;
  sortDir: SortDir;
  onToggleSort: () => void;
  description: DescriptionEditor;
  admin: AdminToggler;
  mode: ModeEditor;
  accessVlan: AccessVlanEditor;
  trunkVlans: TrunkVlansEditor;
}

export function PortTable({
  pageItems,
  canEdit,
  selectedPorts,
  onToggleSelect,
  onSelectAllVisible,
  allPageSelected,
  somePageSelected,
  busy,
  sortDir,
  onToggleSort,
  description,
  admin,
  mode,
  accessVlan,
  trunkVlans,
}: PortTableProps) {
  return (
    <div className="overflow-auto max-h-[calc(100vh-380px)] border border-gray-200 rounded-md">
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 z-10 bg-gray-50">
          <tr className="border-b border-gray-200">
            <th className="px-3 py-2 w-8">
              {canEdit && (
                <input
                  type="checkbox"
                  checked={allPageSelected}
                  ref={(el) => { if (el) el.indeterminate = somePageSelected && !allPageSelected; }}
                  onChange={(e) => onSelectAllVisible(e.target.checked)}
                  disabled={busy.bulkExecuting}
                  className="w-4 h-4 accent-blue-600 cursor-pointer disabled:cursor-not-allowed"
                  aria-label="Select all visible ports"
                />
              )}
            </th>
            <th
              className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap cursor-pointer select-none hover:bg-gray-100"
              onClick={onToggleSort}
              aria-sort={sortDir === 'asc' ? 'ascending' : 'descending'}
            >
              Interface <span className="text-gray-400 text-xs">{sortDir === 'asc' ? '↑' : '↓'}</span>
            </th>
            <th className="text-left px-3 py-2 font-medium text-gray-700">Description</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Admin</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Link</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Mode</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Access VLAN</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700">Allowed VLANs</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">PoE</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Speed</th>
            <th className="text-left px-3 py-2 font-medium text-gray-700 whitespace-nowrap">Duplex</th>
          </tr>
        </thead>
        <tbody>
          {pageItems.map((port) => (
            <PortRow
              key={port.name}
              port={port}
              canEdit={canEdit}
              selected={selectedPorts.has(port.name)}
              onToggleSelect={onToggleSelect}
              busy={busy}
              description={description}
              admin={admin}
              mode={mode}
              accessVlan={accessVlan}
              trunkVlans={trunkVlans}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}
