'use client';

import type { Port } from '@/types/port';
import { DASH } from '../helpers';
import type { AccessVlanEditor, EditorsBusy } from '../types';
import { RowSpinner } from './StatusBadges';

interface VlanConfigFormProps {
  port: Port;
  canEdit: boolean;
  busy: EditorsBusy;
  editor: AccessVlanEditor;
}

// Renders the access-VLAN / native-VLAN value for a port. When edit mode is
// active for this row it expands into a small inline form (the "VLAN config
// form" named in the refactor roadmap §13.1.5).
export function VlanConfigForm({ port, canEdit, busy, editor }: VlanConfigFormProps) {
  const isEditing = editor.editingPort === port.name;
  const isSaving = editor.savingPort === port.name;

  if (isEditing) {
    return (
      <div className="flex flex-col gap-1 min-w-[140px]">
        <div className="flex items-center gap-1">
          <input
            type="number"
            min={1}
            max={4094}
            value={editor.value}
            onChange={(e) => editor.setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); editor.save(port); }
              if (e.key === 'Escape') { e.preventDefault(); editor.cancel(); }
            }}
            disabled={isSaving}
            autoFocus
            placeholder="1–4094"
            aria-label={`Access VLAN for ${port.name}`}
            className="w-20 border border-gray-300 rounded px-2 py-1 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <button
            onClick={() => editor.save(port)}
            disabled={isSaving}
            className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSaving ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
          </button>
          <button
            onClick={editor.cancel}
            disabled={isSaving}
            className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
        </div>
        {editor.errorPort?.port === port.name && (
          <p className="text-xs text-red-600">{editor.errorPort.message}</p>
        )}
      </div>
    );
  }

  const canTriggerEdit = canEdit && (port.mode === 'access' || port.mode === 'trunk');
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-mono text-xs">{port.access_vlan ?? DASH}</span>
      {canTriggerEdit && (
        <button
          onClick={() => editor.start(port)}
          disabled={busy.bulkExecuting || busy.anyEditing || busy.anySaving}
          className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
          aria-label={`Edit ${port.mode === 'trunk' ? 'native VLAN' : 'access VLAN'} for ${port.name}`}
          title={port.mode === 'trunk' ? 'Edit native VLAN (PVID)' : 'Edit access VLAN'}
        >
          ✎
        </button>
      )}
    </div>
  );
}
