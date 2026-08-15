'use client';

import type { Port, TrunkVlanMode } from '@/types/port';
import { formatVlanList, parseVlanInput } from '../helpers';
import type { EditorsBusy, TrunkVlansEditor } from '../types';
import { RowSpinner } from './StatusBadges';

interface TrunkVlansCellProps {
  port: Port;
  canEdit: boolean;
  busy: EditorsBusy;
  editor: TrunkVlansEditor;
}

export function TrunkVlansCell({ port, canEdit, busy, editor }: TrunkVlansCellProps) {
  const isEditing = editor.editingPort === port.name;
  const isSaving = editor.savingPort === port.name;

  if (isEditing) {
    const hasError = editor.errorPort?.port === port.name;
    return (
      <div className="flex flex-col gap-1.5 min-w-[260px]">
        <div className="flex items-center gap-1">
          <select
            value={editor.mode}
            onChange={(e) => editor.setMode(e.target.value as TrunkVlanMode)}
            disabled={isSaving}
            className="px-1.5 py-1 text-xs border border-gray-300 rounded bg-white focus:outline-none focus:ring-1 focus:ring-blue-400 disabled:opacity-50"
            aria-label="Operation mode"
          >
            <option value="replace">Replace</option>
            <option value="add">Add</option>
            <option value="remove">Remove</option>
          </select>
          <input
            type="text"
            value={editor.value}
            onChange={(e) => {
              editor.setValue(e.target.value);
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
            autoFocus
            placeholder="e.g. 10,20,30-35"
            aria-label={`Trunk VLANs for ${port.name}`}
            className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => editor.save(port)}
            disabled={isSaving || (hasError && !!editor.errorPort?.message && editor.value.trim() !== '')}
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
      <span className="truncate font-mono text-xs" title={formatVlanList(port.allowed_vlans)}>
        {formatVlanList(port.allowed_vlans)}
      </span>
      {canEdit && port.mode === 'trunk' && (
        <button
          onClick={() => editor.start(port)}
          disabled={busy.bulkExecuting || busy.anyEditing || busy.anySaving}
          className="opacity-60 hover:opacity-100 text-gray-500 hover:text-purple-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed flex-shrink-0"
          aria-label={`Edit trunk VLANs for ${port.name}`}
          title="Edit trunk allowed VLANs"
        >
          ✎
        </button>
      )}
    </div>
  );
}
