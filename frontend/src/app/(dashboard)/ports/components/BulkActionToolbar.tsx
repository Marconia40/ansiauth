'use client';

import type { BulkOps } from '../types';

interface BulkActionToolbarProps {
  bulk: BulkOps;
}

export function BulkActionToolbar({ bulk }: BulkActionToolbarProps) {
  if (bulk.selectedPorts.size === 0) return null;

  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 bg-blue-50 rounded-md border border-blue-200">
        <span className="text-sm font-medium text-blue-800 whitespace-nowrap">
          {bulk.selectedPorts.size} port{bulk.selectedPorts.size !== 1 ? 's' : ''} selected
        </span>
        {bulk.executing ? (
          <span className="text-xs text-blue-600 ml-1 animate-pulse">Executing…</span>
        ) : (
          <div className="flex flex-wrap items-start gap-1.5 ml-1">
            <button
              onClick={() => bulk.runAction('enable')}
              className="px-2.5 py-1 text-xs text-green-700 border border-green-300 rounded hover:bg-green-50"
            >
              Enable
            </button>
            {bulk.confirmingDisable ? (
              <div className="flex items-center gap-1">
                <span className="text-xs text-red-600 whitespace-nowrap">
                  Disable {bulk.selectedPorts.size} port{bulk.selectedPorts.size !== 1 ? 's' : ''}?
                </span>
                <button
                  onClick={() => { bulk.setConfirmingDisable(false); bulk.runAction('disable'); }}
                  className="px-1.5 py-0.5 text-xs bg-red-600 text-white rounded hover:bg-red-700"
                >
                  Yes
                </button>
                <button
                  onClick={() => bulk.setConfirmingDisable(false)}
                  className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => bulk.setConfirmingDisable(true)}
                className="px-2.5 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50"
              >
                Disable
              </button>
            )}

            <div className="flex flex-col gap-0.5">
              {bulk.vlanExpanded ? (
                <div className="flex items-center gap-1">
                  <input
                    type="number"
                    min={1}
                    max={4094}
                    value={bulk.vlanInput}
                    onChange={(e) => { bulk.setVlanInput(e.target.value); bulk.setErrors([]); }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); bulk.setAccessVlan(); }
                      if (e.key === 'Escape') {
                        e.preventDefault();
                        bulk.setVlanExpanded(false);
                        bulk.setVlanInput('');
                        bulk.setErrors([]);
                      }
                    }}
                    autoFocus
                    placeholder="1–4094"
                    className="w-20 border border-gray-300 rounded px-2 py-0.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
                  />
                  <button
                    onClick={() => bulk.setAccessVlan()}
                    disabled={!bulk.vlanInput.trim()}
                    className="px-2 py-0.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    Apply
                  </button>
                  <button
                    onClick={() => { bulk.setVlanExpanded(false); bulk.setVlanInput(''); bulk.setErrors([]); }}
                    className="px-2 py-0.5 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50"
                  >
                    ×
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => { bulk.setVlanExpanded(true); bulk.setErrors([]); }}
                  disabled={bulk.hasNonAccessSelected}
                  className="px-2.5 py-1 text-xs text-blue-700 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-40 disabled:cursor-not-allowed"
                  title={bulk.hasNonAccessSelected ? 'All selected ports must be in access mode' : 'Set access VLAN on selected ports'}
                >
                  Set Access VLAN
                </button>
              )}
              {bulk.hasNonAccessSelected && !bulk.vlanExpanded && (
                <span className="text-xs text-gray-400">Non-access ports in selection</span>
              )}
            </div>

            {bulk.confirmingClearDesc ? (
              <div className="flex items-center gap-1">
                <span className="text-xs text-gray-600 whitespace-nowrap">
                  Clear desc on {bulk.selectedPorts.size} port{bulk.selectedPorts.size !== 1 ? 's' : ''}?
                </span>
                <button
                  onClick={() => { bulk.setConfirmingClearDesc(false); bulk.runAction('clear-description'); }}
                  className="px-1.5 py-0.5 text-xs bg-gray-700 text-white rounded hover:bg-gray-800"
                >
                  Yes
                </button>
                <button
                  onClick={() => bulk.setConfirmingClearDesc(false)}
                  className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50"
                >
                  No
                </button>
              </div>
            ) : (
              <button
                onClick={() => bulk.setConfirmingClearDesc(true)}
                className="px-2.5 py-1 text-xs text-gray-700 border border-gray-300 rounded hover:bg-gray-50"
              >
                Clear Description
              </button>
            )}
            <button
              onClick={() => {
                bulk.setSelectedPorts(new Set());
                bulk.setVlanExpanded(false);
                bulk.setVlanInput('');
                bulk.setErrors([]);
                bulk.setConfirmingDisable(false);
                bulk.setConfirmingClearDesc(false);
              }}
              className="px-2.5 py-1 text-xs text-gray-500 border border-gray-200 rounded hover:bg-gray-100"
            >
              Clear Selection
            </button>
          </div>
        )}
      </div>
      {bulk.errors.length > 0 && (
        <div className="mt-1.5 px-3 py-2 bg-red-50 border border-red-200 rounded-md">
          <p className="text-xs font-medium text-red-700 mb-1">
            {bulk.errors.length} error{bulk.errors.length !== 1 ? 's' : ''} during bulk operation:
          </p>
          <ul className="text-xs text-red-600 space-y-0.5 max-h-24 overflow-y-auto">
            {bulk.errors.map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}
