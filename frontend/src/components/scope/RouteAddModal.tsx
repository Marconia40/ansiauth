'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { addGlobalConfigRoute } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

interface Props {
  open: boolean;
  onClose: () => void;
  deviceName: string;
}

// POST /devices/{name}/global-config/routes (RF-GLOBAL-06). `destination`
// is normalized to its network address on the backend, so "192.168.99.5/24"
// and "192.168.99.0/24" are treated as the same route.  If the destination
// already exists with a DIFFERENT next-hop the backend will NOT overwrite
// -- it responds `accion="ruta_ya_existe"` and leaves the device untouched
// (SRS alternative course). The user has to remove the old route first;
// we hint that in the modal so the failure mode isn't a mystery.
export function RouteAddModal({ open, onClose, deviceName }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [destination, setDestination] = useState('');
  const [nextHop, setNextHop] = useState('');
  const [error, setError] = useState<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setDestination('');
    setNextHop('');
    setError(null);
  }, [open]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const dTrim = destination.trim();
  const nhTrim = nextHop.trim();
  // Very loose client-side checks — the backend does the real
  // validation via `ipaddress.ip_network` / `ipaddress.ip_address`.
  const destinationLooksCidr = /\//.test(dTrim);
  const canSubmit = dTrim !== '' && nhTrim !== '';

  const mutation = useMutation({
    mutationFn: () =>
      addGlobalConfigRoute(deviceName, {
        destination: dTrim,
        next_hop: nhTrim,
      }),
    onSuccess: (result) => {
      trackGroupJob(
        result.group_job_id,
        `Add route ${dTrim} → ${nhTrim} on ${deviceName}`,
      );
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      onClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Failed to queue the route addition.'));
    },
  });

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : onClose}
      title={`Add static route — ${deviceName}`}
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary
            onClick={() => mutation.mutate()}
            disabled={!canSubmit || mutation.isPending}
          >
            {mutation.isPending ? 'Applying…' : 'Add'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="Destination (CIDR)">
          <input
            type="text"
            value={destination}
            onChange={(e) => setDestination(e.target.value)}
            placeholder="e.g. 192.168.99.0/24"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          {dTrim !== '' && !destinationLooksCidr && (
            <p className="text-xs text-danger mt-1">
              Destination must include a prefix length, e.g. /24.
            </p>
          )}
        </FieldRow>

        <FieldRow label="Next-hop IP">
          <input
            type="text"
            value={nextHop}
            onChange={(e) => setNextHop(e.target.value)}
            placeholder="e.g. 10.0.0.1"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>

        <p className="text-xs text-muted">
          If a route with the same destination already exists but with a
          different next-hop, the backend will NOT overwrite it — remove
          the old route first, then add the new one.
        </p>

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
