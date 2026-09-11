'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { updateVlan, parseFieldErrors } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import type { VlanRow } from './scopeVlans';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import {
  FieldRow,
  FieldError,
  ModalPrimary,
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

export function VlanUpdateModal({
  open,
  onClose,
  scope,
  deviceName,
  rows,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [pickedId, setPickedId] = useState<number | ''>('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);

  const picked = useMemo(
    () => rows.find((r) => r.vlanId === pickedId) ?? null,
    [rows, pickedId],
  );

  // When the picked VLAN changes, pre-fill the form with its current values.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!picked) {
      setDescription('');
      setSelected(new Set());
      return;
    }
    setDescription(picked.name);
    setSelected(new Set(picked.devices));
  }, [picked]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const effectiveSelected = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selected;
  }, [scope, deviceName, selected]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (pickedId === '') throw new Error('No VLAN picked');
      return updateVlan(pickedId, {
        description,
        devices: Array.from(effectiveSelected),
      });
    },
    onSuccess: (result) => {
      trackGroupJob(
        result.group_job_id,
        `Update VLAN ${pickedId} on ${effectiveSelected.size} device(s)`,
      );
      invalidateVlanQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) => {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setPickedId('');
    setDescription('');
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    onClose();
  }

  const canSubmit =
    pickedId !== '' && effectiveSelected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Update VLAN"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="VLAN to update">
          <select
            value={pickedId}
            onChange={(e) =>
              setPickedId(e.target.value === '' ? '' : Number(e.target.value))
            }
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          >
            <option value="">— pick a VLAN —</option>
            {rows.map((row) => (
              <option key={row.vlanId} value={row.vlanId}>
                {row.vlanId} — {row.name || '(no name)'}
              </option>
            ))}
          </select>
        </FieldRow>

        {picked && (
          <>
            <FieldRow label="Description">
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              />
              <FieldError message={fieldErrors?.description} />
            </FieldRow>

            <FieldRow label="Target devices">
              <DeviceSelector
                scope={scope}
                value={effectiveSelected}
                onChange={setSelected}
                deviceName={deviceName}
              />
              <p className="text-xs text-muted mt-1">
                Devices currently carrying this VLAN are pre-selected.
              </p>
            </FieldRow>
          </>
        )}

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
