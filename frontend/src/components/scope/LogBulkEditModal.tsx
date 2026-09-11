'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  addGlobalConfigLogServer,
  removeGlobalConfigLogServer,
  parseFieldErrors,
} from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import { Modal } from './Modal';
import { DeviceSelector } from './DeviceSelector';
import { ModeToggle } from './NtpBulkEditModal';
import {
  FieldRow,
  FieldError,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

class LogBulkEditError extends Error {
  fieldErrors: Record<string, string> | null;
  constructor(message: string, fieldErrors: Record<string, string> | null) {
    super(message);
    this.fieldErrors = fieldErrors;
  }
}

type Mode = 'add' | 'remove';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
}

// Bulk version of LoggingEditModal.tsx. Same Add/Remove split as
// NtpBulkEditModal.tsx; Add carries the optional device-wide `level` field
// the single-device modal also has. No bulk endpoint — N independent
// per-device calls, each already async/job-based.
export function LogBulkEditModal({ open, onClose, scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [mode, setMode] = useState<Mode>('add');
  const [server, setServer] = useState('');
  const [level, setLevel] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const trimmedServer = server.trim();
  const trimmedLevel = level.trim();

  const mutation = useMutation({
    mutationFn: async () => {
      const devices = Array.from(selected);
      setProgress({ done: 0, total: devices.length });
      let done = 0;
      const results = await Promise.allSettled(
        devices.map(async (d) => {
          try {
            return mode === 'add'
              ? await addGlobalConfigLogServer(d, {
                  server: trimmedServer,
                  ...(trimmedLevel !== '' ? { level: trimmedLevel } : {}),
                })
              : await removeGlobalConfigLogServer(d, { server: trimmedServer });
          } finally {
            done += 1;
            setProgress({ done, total: devices.length });
          }
        }),
      );

      const label =
        mode === 'add'
          ? trimmedLevel !== ''
            ? `Add log ${trimmedServer} (level ${trimmedLevel}) on ${devices.length} device(s)`
            : `Add log ${trimmedServer} on ${devices.length} device(s)`
          : `Remove log ${trimmedServer} on ${devices.length} device(s)`;
      for (const r of results) {
        if (r.status === 'fulfilled') {
          trackGroupJob(r.value.group_job_id, label);
        }
      }

      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length > 0) {
        const firstErr = (failed[0] as PromiseRejectedResult).reason;
        throw new LogBulkEditError(
          `${failed.length} of ${devices.length} device(s) failed: ${extractMessage(firstErr, 'Update failed.')}`,
          parseFieldErrors(firstErr),
        );
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] });
      resetAndClose();
    },
    onError: (err: unknown) => {
      setFieldErrors(err instanceof LogBulkEditError ? err.fieldErrors : null);
      setError(extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setMode('add');
    setServer('');
    setLevel('');
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    setProgress(null);
    onClose();
  }

  const canSubmit = trimmedServer !== '' && selected.size > 0 && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : resetAndClose}
      title="Bulk edit log servers"
      footer={
        <>
          <ModalSecondary onClick={resetAndClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? progress
                ? `${mode === 'add' ? 'Adding' : 'Removing'} ${progress.done}/${progress.total}…`
                : 'Applying…'
              : mode === 'add'
              ? 'Add'
              : 'Remove'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <ModeToggle mode={mode} onChange={setMode} />

        <FieldRow label="Server IP">
          <input
            type="text"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="e.g. 10.0.0.10"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
          <FieldError message={fieldErrors?.server} />
        </FieldRow>

        {mode === 'add' && (
          <FieldRow label="Level (optional, device-wide)">
            <input
              type="text"
              value={level}
              onChange={(e) => setLevel(e.target.value)}
              placeholder="e.g. informational"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
            <FieldError message={fieldErrors?.level} />
          </FieldRow>
        )}

        <FieldRow label="Target devices">
          <DeviceSelector scope={scope} value={selected} onChange={setSelected} />
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
