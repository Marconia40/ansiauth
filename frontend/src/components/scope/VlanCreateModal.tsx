'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createVlan } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
  /** Only relevant at device scope. */
  deviceName?: string;
}

export function VlanCreateModal({ open, onClose, scope, deviceName }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [vlanId, setVlanId] = useState('');
  const [name, setName] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  // Device scope: the modal shouldn't offer a picker — the current device is
  // always the target. We keep `selected` in sync so the submit body includes it.
  const effectiveSelected = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selected;
  }, [scope, deviceName, selected]);

  const mutation = useMutation({
    mutationFn: async () => {
      const parsedId = Number(vlanId);
      const devices = Array.from(effectiveSelected);
      return createVlan({ vlan_id: parsedId, name: name.trim(), devices });
    },
    onSuccess: (result) => {
      trackGroupJob(
        result.group_job_id,
        `Create VLAN ${vlanId} on ${effectiveSelected.size} device(s)`,
      );
      invalidateVlanQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Create failed.'));
    },
  });

  function resetAndClose() {
    setVlanId('');
    setName('');
    setSelected(new Set());
    setError(null);
    onClose();
  }

  const parsedId = Number(vlanId);
  const idValid = Number.isInteger(parsedId) && parsedId >= 1 && parsedId <= 4094;
  const canSubmit =
    idValid && name.trim().length > 0 && effectiveSelected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Create VLAN"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending ? 'Creating…' : 'Create'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="VLAN ID">
          <input
            type="number"
            min={1}
            max={4094}
            value={vlanId}
            onChange={(e) => setVlanId(e.target.value)}
            placeholder="e.g. 10"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          {!idValid && vlanId !== '' && (
            <p className="text-xs text-danger mt-1">
              VLAN id must be an integer between 1 and 4094.
            </p>
          )}
        </FieldRow>

        <FieldRow label="Name">
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. MANAGEMENT"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>

        <FieldRow label="Target devices">
          <DeviceSelector
            scope={scope}
            value={effectiveSelected}
            onChange={setSelected}
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

// ── Reused primitives ────────────────────────────────────────────────────────

export function FieldRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wider text-muted">
        {label}
      </span>
      {children}
    </label>
  );
}

export function ModalPrimary({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-info px-4 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

export function ModalSecondary({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md border border-panel-border bg-panel-elev px-4 py-1.5 text-sm font-semibold text-text hover:bg-panel-elev/80 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

export function ModalDanger({
  onClick,
  disabled,
  children,
}: {
  onClick?: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="rounded-md bg-danger px-4 py-1.5 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-40 disabled:cursor-not-allowed transition"
    >
      {children}
    </button>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

export function invalidateVlanQueries(queryClient: ReturnType<typeof useQueryClient>) {
  // Invalidate every device-scoped VLAN envelope so the tables refetch. We
  // don't know exactly which devices were touched from here, but React Query
  // is happy to re-run only the queries that are actually mounted.
  queryClient.invalidateQueries({ queryKey: ['vlans', 'synced'] });
}

export function extractMessage(err: unknown, fallback: string): string {
  const e = err as {
    response?: { data?: { detail?: string; message?: string } };
    message?: string;
  } | null;
  return (
    e?.response?.data?.detail ??
    e?.response?.data?.message ??
    e?.message ??
    fallback
  );
}
