'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { batchVlans } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import type { VlanRow } from './scopeVlans';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import {
  FieldRow,
  ModalDanger,
  ModalSecondary,
  extractMessage,
  invalidateVlanQueries,
} from './VlanCreateModal';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
  deviceName?: string;
  rows: VlanRow[];
}

export function VlanRemoveModal({
  open,
  onClose,
  scope,
  deviceName,
  rows,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [pickedIds, setPickedIds] = useState<Set<number>>(new Set());
  const [selectedDevices, setSelectedDevices] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const effectiveDevices = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selectedDevices;
  }, [scope, deviceName, selectedDevices]);

  const mutation = useMutation({
    mutationFn: async () => {
      const ids = Array.from(pickedIds);
      const devices = Array.from(effectiveDevices);
      // One batch call: N deletions × M devices lands as M jobs (one per
      // device), each running every deletion in a single SSH session --
      // replacing the earlier N × M loop that created N × M jobs.
      return batchVlans({
        changes: ids.map((id) => ({ vlan_id: id, eliminar: true })),
        devices,
      });
    },
    onSuccess: (result) => {
      const label =
        pickedIds.size === 1
          ? `Remove VLAN from ${effectiveDevices.size} device(s)`
          : `Remove ${pickedIds.size} VLANs from ${effectiveDevices.size} device(s)`;
      trackGroupJob(result.group_job_id, label);
      invalidateVlanQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) =>
      setError(extractMessage(err, 'Remove failed.')),
  });

  function resetAndClose() {
    setPickedIds(new Set());
    setSelectedDevices(new Set());
    setError(null);
    onClose();
  }

  function toggleId(id: number) {
    const next = new Set(pickedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setPickedIds(next);
  }

  const canSubmit =
    pickedIds.size > 0 && effectiveDevices.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Remove VLANs"
      widthClass="w-full max-w-xl"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalDanger onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? 'Removing…'
              : `Remove ${pickedIds.size || ''}`.trim()}
          </ModalDanger>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="Pick VLANs to remove">
          <div className="rounded-md border border-panel-border max-h-56 overflow-y-auto divide-y divide-panel-border">
            {rows.length === 0 ? (
              <p className="p-3 text-sm italic text-muted">
                No VLANs in this scope.
              </p>
            ) : (
              rows.map((row) => (
                <label
                  key={row.vlanId}
                  className="flex items-center gap-3 px-3 py-1.5 text-sm text-text cursor-pointer hover:bg-panel-elev/60"
                >
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-danger"
                    checked={pickedIds.has(row.vlanId)}
                    onChange={() => toggleId(row.vlanId)}
                  />
                  <span className="font-semibold tabular-nums w-14">
                    {row.vlanId}
                  </span>
                  <span className="text-muted truncate">
                    {row.name || '(no name)'}
                  </span>
                </label>
              ))
            )}
          </div>
        </FieldRow>

        <FieldRow label="Remove from devices">
          <DeviceSelector
            scope={scope}
            value={effectiveDevices}
            onChange={setSelectedDevices}
            deviceName={deviceName}
          />
        </FieldRow>

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
