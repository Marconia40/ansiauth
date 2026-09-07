'use client';

import { useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { createSVI } from '@/services/api';
import type { Scope } from './ScopeDashboard';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
  /** Only relevant at device scope. */
  deviceName?: string;
  onDone?: (msg: string, tone: 'ok' | 'error') => void;
}

/** Crear SVI. A diferencia de VLAN, la API es one-device-per-call:
 * cada device tiene su propia SVI con IPs independientes. Multi-device
 * se resuelve con Promise.all en paralelo. Backend valida que la VLAN
 * exista en el device (RF-INTERV-09) -- si no existe, el error del
 * backend es lo que se ve. */
export function SVICreateModal({ open, onClose, scope, deviceName, onDone }: Props) {
  const queryClient = useQueryClient();

  const [vlanId, setVlanId] = useState('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(
    null,
  );

  const effectiveSelected = useMemo(() => {
    if (scope.kind === 'device' && deviceName) return new Set([deviceName]);
    return selected;
  }, [scope, deviceName, selected]);

  const mutation = useMutation({
    mutationFn: async () => {
      const parsedId = Number(vlanId);
      const devices = Array.from(effectiveSelected);
      const desc = description.trim();
      const body = { vlan_id: parsedId, description: desc || null };

      setProgress({ done: 0, total: devices.length });
      let done = 0;
      const results = await Promise.allSettled(
        devices.map(async (d) => {
          try {
            return await createSVI(d, body);
          } finally {
            done += 1;
            setProgress({ done, total: devices.length });
          }
        }),
      );
      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length > 0) {
        // Mensaje del primer error para dar señal accionable en vez del
        // genérico "N failed". El resto del detalle igual queda en el
        // console si hace falta debug.
        const firstErr = (failed[0] as PromiseRejectedResult).reason;
        throw new Error(
          `${failed.length} of ${devices.length} device(s) failed: ${extractMessage(firstErr, 'Create failed.')}`,
        );
      }
    },
    onSuccess: () => {
      onDone?.(
        `SVI ${vlanId} created on ${effectiveSelected.size} device(s).`,
        'ok',
      );
      invalidateSviQueries(queryClient);
      resetAndClose();
    },
    onError: (err: unknown) => setError(extractMessage(err, 'Create failed.')),
  });

  function resetAndClose() {
    setVlanId('');
    setDescription('');
    setSelected(new Set());
    setError(null);
    setProgress(null);
    onClose();
  }

  const parsedId = Number(vlanId);
  const idValid = Number.isInteger(parsedId) && parsedId >= 1 && parsedId <= 4094;
  const canSubmit =
    idValid && effectiveSelected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={resetAndClose}
      title="Create virtual interface"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? progress
                ? `Creating ${progress.done}/${progress.total}…`
                : 'Creating…'
              : 'Create'}
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
          <p className="text-xs text-muted mt-1">
            The VLAN must already exist on the target device — the backend
            will reject the create otherwise.
          </p>
        </FieldRow>

        <FieldRow label="Description (optional)">
          <input
            type="text"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. Management SVI"
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

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Invalida las envelopes SVI para forzar refetch después de una write. */
export function invalidateSviQueries(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: ['svis', 'synced'] });
}
