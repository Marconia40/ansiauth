'use client';

import type { Port } from '@/types/port';
import { DASH } from '../helpers';
import type { DescriptionEditor, EditorsBusy } from '../types';
import { RowSpinner } from './StatusBadges';

interface DescriptionCellProps {
  port: Port;
  canEdit: boolean;
  busy: EditorsBusy;
  editor: DescriptionEditor;
}

export function DescriptionCell({ port, canEdit, busy, editor }: DescriptionCellProps) {
  const isEditing = editor.editingPort === port.name;
  const isSaving = editor.savingPort === port.name;

  if (isEditing) {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1">
          <input
            type="text"
            value={editor.editingValue}
            onChange={(e) => editor.setEditingValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); editor.save(port); }
              if (e.key === 'Escape') { e.preventDefault(); editor.cancel(); }
            }}
            disabled={isSaving}
            maxLength={200}
            autoFocus
            placeholder="(empty clears description)"
            aria-label={`Description for ${port.name}`}
            className="flex-1 min-w-0 border border-gray-300 rounded px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
          />
          <button
            onClick={() => editor.save(port)}
            disabled={isSaving}
            className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            aria-label="Save description"
          >
            {isSaving ? <span className="inline-flex items-center gap-1"><RowSpinner />Saving</span> : 'Save'}
          </button>
          <button
            onClick={editor.cancel}
            disabled={isSaving}
            className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            aria-label="Cancel"
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

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-gray-900 truncate" title={port.description ?? ''}>
        {port.description ?? DASH}
      </span>
      {canEdit && (
        <button
          onClick={() => editor.start(port)}
          disabled={busy.bulkExecuting || busy.anyEditing || busy.anySaving}
          className="opacity-60 hover:opacity-100 text-gray-500 hover:text-blue-600 text-xs px-1 disabled:opacity-30 disabled:cursor-not-allowed"
          aria-label={`Edit description for ${port.name}`}
          title="Edit description"
        >
          ✎
        </button>
      )}
    </div>
  );
}
