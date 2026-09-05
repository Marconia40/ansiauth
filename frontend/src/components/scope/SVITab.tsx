'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { refreshDeviceSvis } from '@/services/api';
import type { Scope } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { SVITable } from './SVITable';
import { useScopeSvis, type SviRow } from './scopeSvis';
import { SVICreateModal } from './SVICreateModal';
import { SVIRemoveModal } from './SVIRemoveModal';
import { SVIEditModal } from './SVIEditModal';

interface Props {
  scope: Scope;
  /** Only meaningful at device scope. */
  deviceName?: string;
}

type Toast = { msg: string; tone: 'ok' | 'error' } | null;

export function SVITab({ scope, deviceName }: Props) {
  const queryClient = useQueryClient();
  const { rows, devices, isLoading, isError } = useScopeSvis(scope);

  const [openCreate, setOpenCreate] = useState(false);
  const [openRemove, setOpenRemove] = useState(false);
  const [editRow, setEditRow] = useState<SviRow | null>(null);
  const [toast, setToast] = useState<Toast>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 4500);
    return () => clearTimeout(t);
  }, [toast]);

  async function handleRefresh() {
    const names = devices.map((d) => d.name);
    setRefreshing(true);
    try {
      await Promise.all(names.map((n) => refreshDeviceSvis(n)));
    } finally {
      for (const n of names) {
        queryClient.invalidateQueries({ queryKey: ['svis', 'synced', n] });
      }
      setRefreshing(false);
    }
  }

  const emptyScope = devices.length === 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshing}
          disabled={emptyScope}
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <ActionButton onClick={() => setOpenCreate(true)} disabled={emptyScope}>
          Create SVI
        </ActionButton>
        <ActionButton
          onClick={() => setOpenRemove(true)}
          disabled={emptyScope || rows.length === 0}
          tone="danger"
        >
          Remove SVI
        </ActionButton>
      </div>

      <Panel title="Virtual Interfaces (SVIs)">
        <SVITable
          scope={scope}
          rows={rows}
          isLoading={isLoading}
          isError={isError}
          onEdit={setEditRow}
        />
      </Panel>

      <SVICreateModal
        open={openCreate}
        onClose={() => setOpenCreate(false)}
        scope={scope}
        deviceName={deviceName}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />
      <SVIRemoveModal
        open={openRemove}
        onClose={() => setOpenRemove(false)}
        scope={scope}
        rows={rows}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />
      <SVIEditModal
        open={editRow !== null}
        onClose={() => setEditRow(null)}
        row={editRow}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />

      {toast && (
        <div
          className={`fixed bottom-4 right-4 z-40 rounded-md px-4 py-2 shadow-lg text-sm ${
            toast.tone === 'ok'
              ? 'bg-success text-white'
              : 'bg-danger text-white'
          }`}
        >
          {toast.msg}
        </div>
      )}
    </div>
  );
}

function ActionButton({
  onClick,
  disabled,
  tone = 'default',
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone?: 'default' | 'danger';
  children: React.ReactNode;
}) {
  const base =
    'rounded-md px-4 py-2 text-sm font-semibold uppercase tracking-wider border transition-colors disabled:opacity-40 disabled:cursor-not-allowed';
  const cls =
    tone === 'danger'
      ? `${base} border-danger/60 text-danger hover:bg-danger/10`
      : `${base} border-panel-border text-text hover:bg-panel-elev`;
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls}>
      {children}
    </button>
  );
}
