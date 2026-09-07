'use client';

import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { setGlobalConfigHostname } from '@/services/api';
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
  /** Current hostname read from the cache — used only as an initial value. */
  currentHostname: string | null;
  onDone?: (msg: string, tone: 'ok' | 'error') => void;
}

// PATCH /devices/{name}/global-config/hostname (RF-GLOBAL-08). Async: the
// device only shows the new hostname after the Celery job finishes. This
// modal fires the request, tracks the returned group/job(s) via the shared
// notification context and invalidates the global-config queries so a manual
// Refresh (or the next automatic poll after the sync completes) pulls the
// new value.
export function HostnameEditModal({
  open,
  onClose,
  deviceName,
  currentHostname,
  onDone,
}: Props) {
  const queryClient = useQueryClient();
  const { trackJob, trackGroupJob } = useJobNotifications();
  const [hostname, setHostname] = useState('');
  const [error, setError] = useState<string | null>(null);

  // Re-hydrate the input whenever the modal (re)opens for a different device
  // or the cached hostname changes.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setHostname(currentHostname ?? '');
    setError(null);
  }, [open, currentHostname]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const trimmed = hostname.trim();
  // Same length bounds as backend GlobalConfigHostnameUpdateRequest (1..63).
  const lengthOk = trimmed.length >= 1 && trimmed.length <= 63;
  const changed = trimmed !== (currentHostname ?? '');

  const mutation = useMutation({
    mutationFn: () => setGlobalConfigHostname(deviceName, { hostname: trimmed }),
    onSuccess: (result) => {
      trackGroupJob(result.group_job_id, `Set hostname on ${deviceName}`);
      for (const j of result.jobs) {
        trackJob(j.job_id, `Set hostname on ${deviceName}`, j.device);
      }
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'version', 'synced', deviceName],
      });
      onDone?.(`Hostname change queued on ${deviceName}.`, 'ok');
      onClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Failed to queue the hostname change.'));
    },
  });

  const canSubmit = lengthOk && changed && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : onClose}
      title={`Edit hostname — ${deviceName}`}
      footer={
        <>
          <ModalSecondary onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary
            onClick={() => mutation.mutate()}
            disabled={!canSubmit}
          >
            {mutation.isPending ? 'Applying…' : 'Apply'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="Hostname">
          <input
            type="text"
            value={hostname}
            onChange={(e) => setHostname(e.target.value)}
            placeholder="e.g. sw-core-01"
            maxLength={63}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          {!lengthOk && trimmed.length > 0 && (
            <p className="text-xs text-danger mt-1">
              Hostname must be 1..63 characters.
            </p>
          )}
          {!changed && trimmed.length > 0 && (
            <p className="text-xs text-muted mt-1">
              Same as the current hostname — nothing to apply.
            </p>
          )}
        </FieldRow>

        <p className="text-xs text-muted">
          The change is applied asynchronously; watch the notifications
          panel for job completion, then Refresh to see the new value.
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
