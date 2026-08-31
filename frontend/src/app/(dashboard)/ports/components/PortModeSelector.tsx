'use client';

import type { Port } from '@/types/port';
import { parseVlanInput } from '../helpers';
import type { EditorsBusy, ModeEditor } from '../types';
import { ModeBadge, RowSpinner } from './StatusBadges';

interface PortModeSelectorProps {
  port: Port;
  canEdit: boolean;
  busy: EditorsBusy;
  editor: ModeEditor;
}

// Composite editor that switches a port between access / trunk and sets the
// associated VLAN(s) in a single submit. Hidden behind the mode-cell ✎ icon
// in the table; expands into an inline mini-form when active.
export function PortModeSelector({ port, canEdit, busy, editor }: PortModeSelectorProps) {
  const isEditing = editor.editingPort === port.name;
  const isSaving = editor.savingPort === port.name;
  const hasError = editor.errorPort?.port === port.name;

  if (isEditing) {
    return (
      <div className="flex flex-col gap-1.5 min-w-[280px]">
        <div className="flex items-center gap-1.5 flex-wrap">
          <select
            value={editor.mode}
            onChange={(e) => {
              editor.setMode(e.target.value as 'access' | 'trunk');
              editor.setError(null);
            }}
            disabled={isSaving}
            className="px-1.5 py-1 text-xs border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
            aria-label="Port mode"
          >
            <option value="access">Access</option>
            <option value="trunk">Trunk</option>
          </select>
          <span className="text-xs text-gray-500 whitespace-nowrap">
            {editor.mode === 'trunk' ? 'Native VLAN / PVID:' : 'Access VLAN:'}
          </span>
          <input
            type="number"
            min={1}
            max={4094}
            value={editor.accessVlan}
            onChange={(e) => {
              editor.setAccessVlan(e.target.value);
              editor.setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); editor.save(port); }
              if (e.key === 'Escape') { e.preventDefault(); editor.cancel(); }
            }}
            disabled={isSaving}
            autoFocus
            placeholder="1-4094"
            aria-label={editor.mode === 'trunk' ? `Native VLAN for ${port.name}` : `Access VLAN for ${port.name}`}
            className="w-20 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
        </div>
        {editor.mode === 'trunk' && (
          <div className="flex items-center gap-1">
            <input
              type="text"
              value={editor.trunkVlans}
              onChange={(e) => {
                editor.setTrunkVlans(e.target.value);
                const { error } = parseVlanInput(e.target.value);
                if (error && e.target.value.trim()) {
                  editor.setError({ port: port.name, message: error });
                } else {
                  editor.setError(null);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') { e.preventDefault(); editor.save(port); }
                if (e.key === 'Escape') { e.preventDefault(); editor.cancel(); }
              }}
              disabled={isSaving}
              placeholder="e.g. 10,20,30-35"
              aria-label={`Allowed VLANs for ${port.name}`}
              className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            />
          </div>
        )}
        <div className="flex items-center gap-1">
          <button
            onClick={() => editor.save(port)}
            disabled={isSaving || (hasError && !!editor.errorPort?.message)}
            className="px-2 py-0.5 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSaving ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
          </button>
          <button
            onClick={editor.cancel}
            disabled={isSaving}
            className="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Cancel
          </button>
        </div>
        {hasError && (
          <p className="text-xs text-red-600">{editor.errorPort?.message}</p>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5">
      <ModeBadge mode={port.mode} />
      {canEdit && port.mode !== 'unknown' && (
        <button
          onClick={() => editor.start(port)}
          disabled={busy.bulkExecuting || busy.anyEditing || busy.anySaving}
          className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
          aria-label={`Edit mode for ${port.name}`}
          title="Configure port mode and VLANs"
        >
          ✎
        </button>
      )}
    </div>
  );
}
