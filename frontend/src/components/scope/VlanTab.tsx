'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { Scope } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { runScopeRefresh } from './scopeRefresh';
import { VlanTable } from './VlanTable';
import { useScopeVlans } from './scopeVlans';
import { VlanCreateModal } from './VlanCreateModal';
import { VlanUpdateModal } from './VlanUpdateModal';
import { VlanRemoveModal } from './VlanRemoveModal';

interface Props {
  scope: Scope;
  /** Only meaningful at device scope. */
  deviceName?: string;
}

type Toast = { msg: string; tone: 'ok' | 'error' } | null;

export function VlanTab({ scope, deviceName }: Props) {
  const queryClient = useQueryClient();
  const { rows, devices, isLoading, isError } = useScopeVlans(scope);

  const [openCreate, setOpenCreate] = useState(false);
  const [openUpdate, setOpenUpdate] = useState(false);
  const [openRemove, setOpenRemove] = useState(false);
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
    await runScopeRefresh(names);
    for (const n of names) {
      queryClient.invalidateQueries({ queryKey: ['vlans', 'synced', n] });
      queryClient.invalidateQueries({ queryKey: ['ports', 'synced', n] });
    }
    setRefreshing(false);
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
          Create VLAN
        </ActionButton>
        <ActionButton
          onClick={() => setOpenUpdate(true)}
          disabled={emptyScope || rows.length === 0}
        >
          Update VLAN
        </ActionButton>
        <ActionButton
          onClick={() => setOpenRemove(true)}
          disabled={emptyScope || rows.length === 0}
          tone="danger"
        >
          Remove VLAN
        </ActionButton>
      </div>

      <Panel title="Available VLANs">
        <VlanTable
          scope={scope}
          rows={rows}
          isLoading={isLoading}
          isError={isError}
        />
      </Panel>

      <VlanCreateModal
        open={openCreate}
        onClose={() => setOpenCreate(false)}
        scope={scope}
        deviceName={deviceName}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />
      <VlanUpdateModal
        open={openUpdate}
        onClose={() => setOpenUpdate(false)}
        scope={scope}
        deviceName={deviceName}
        rows={rows}
        onDone={(msg, tone) => setToast({ msg, tone })}
      />
      <VlanRemoveModal
        open={openRemove}
        onClose={() => setOpenRemove(false)}
        scope={scope}
        deviceName={deviceName}
        rows={rows}
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
