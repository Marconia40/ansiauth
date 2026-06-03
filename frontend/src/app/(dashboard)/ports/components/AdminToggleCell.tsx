'use client';

import type { Port } from '@/types/port';
import type { AdminToggler } from '../types';
import { AdminBadge, RowSpinner } from './StatusBadges';

interface AdminToggleCellProps {
  port: Port;
  canEdit: boolean;
  bulkExecuting: boolean;
  toggler: AdminToggler;
}

export function AdminToggleCell({ port, canEdit, bulkExecuting, toggler }: AdminToggleCellProps) {
  const isToggling = toggler.togglingPort === port.name;
  const isConfirming = toggler.confirmingPort === port.name;

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <AdminBadge value={port.admin_up} />
        {canEdit && (
          isToggling ? (
            <RowSpinner />
          ) : isConfirming ? (
            <div className="flex items-center gap-1">
              <span className="text-xs text-red-600 whitespace-nowrap">Disable?</span>
              <button
                onClick={() => { toggler.setConfirmingPort(null); toggler.toggle(port); }}
                className="px-1.5 py-0.5 text-xs bg-red-600 text-white rounded hover:bg-red-700"
              >
                Yes
              </button>
              <button
                onClick={() => toggler.setConfirmingPort(null)}
                className="px-1.5 py-0.5 text-xs border border-gray-300 rounded hover:bg-gray-50"
              >
                No
              </button>
            </div>
          ) : (
            <button
              onClick={() =>
                port.admin_up === true
                  ? toggler.setConfirmingPort(port.name)
                  : toggler.toggle(port)
              }
              disabled={bulkExecuting || toggler.togglingPort !== null || port.admin_up === null}
              className={`px-2 py-0.5 text-xs rounded border whitespace-nowrap disabled:opacity-40 disabled:cursor-not-allowed ${
                port.admin_up === null
                  ? 'text-gray-400 border-gray-200'
                  : port.admin_up
                  ? 'text-red-600 border-red-300 hover:bg-red-50'
                  : 'text-green-700 border-green-300 hover:bg-green-50'
              }`}
              aria-label={
                port.admin_up === null
                  ? `Admin state unknown for ${port.name}`
                  : `${port.admin_up ? 'Disable' : 'Enable'} ${port.name}`
              }
              title={
                port.admin_up === null
                  ? 'Admin state unknown'
                  : port.admin_up
                  ? 'Disable interface'
                  : 'Enable interface'
              }
            >
              {port.admin_up === null ? '—' : port.admin_up ? 'Disable' : 'Enable'}
            </button>
          )
        )}
      </div>
      {toggler.errorPort?.port === port.name && (
        <p className="text-xs text-red-600">{toggler.errorPort.message}</p>
      )}
    </div>
  );
}
