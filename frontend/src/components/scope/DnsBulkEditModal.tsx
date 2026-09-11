'use client';

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { addGlobalConfigDns, removeGlobalConfigDns, parseFieldErrors } from '@/services/api';
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

class DnsBulkEditError extends Error {
  fieldErrors: Record<string, string> | null;
  constructor(message: string, fieldErrors: Record<string, string> | null) {
    super(message);
    this.fieldErrors = fieldErrors;
  }
}

type Mode = 'add' | 'remove';
type AddKind = 'server' | 'domain';

interface Props {
  open: boolean;
  onClose: () => void;
  scope: Scope;
}

// Bulk version of DnsEditModal.tsx. Same Add/Remove split as
// NtpBulkEditModal.tsx; Add additionally keeps the server-vs-domain radio
// the single-device modal already has (backend validator: exactly one of
// `server`/`domain_name`). No bulk endpoint — N independent per-device
// calls, each already async/job-based.
export function DnsBulkEditModal({ open, onClose, scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [mode, setMode] = useState<Mode>('add');
  const [addKind, setAddKind] = useState<AddKind>('server');
  const [server, setServer] = useState('');
  const [domain, setDomain] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const trimmedServer = server.trim();
  const trimmedDomain = domain.trim();

  const mutation = useMutation({
    mutationFn: async () => {
      const devices = Array.from(selected);
      setProgress({ done: 0, total: devices.length });
      let done = 0;
      const results = await Promise.allSettled(
        devices.map(async (d) => {
          try {
            if (mode === 'remove') {
              return await removeGlobalConfigDns(d, { server: trimmedServer });
            }
            return addKind === 'server'
              ? await addGlobalConfigDns(d, { server: trimmedServer })
              : await addGlobalConfigDns(d, { domain_name: trimmedDomain });
          } finally {
            done += 1;
            setProgress({ done, total: devices.length });
          }
        }),
      );

      const label =
        mode === 'remove'
          ? `Remove DNS ${trimmedServer} on ${devices.length} device(s)`
          : addKind === 'server'
          ? `Add DNS ${trimmedServer} on ${devices.length} device(s)`
          : `Set domain ${trimmedDomain} on ${devices.length} device(s)`;
      for (const r of results) {
        if (r.status === 'fulfilled') {
          trackGroupJob(r.value.group_job_id, label);
        }
      }

      const failed = results.filter((r) => r.status === 'rejected');
      if (failed.length > 0) {
        const firstErr = (failed[0] as PromiseRejectedResult).reason;
        throw new DnsBulkEditError(
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
      setFieldErrors(err instanceof DnsBulkEditError ? err.fieldErrors : null);
      setError(extractMessage(err, 'Update failed.'));
    },
  });

  function resetAndClose() {
    setMode('add');
    setAddKind('server');
    setServer('');
    setDomain('');
    setSelected(new Set());
    setError(null);
    setFieldErrors(null);
    setProgress(null);
    onClose();
  }

  const canSubmit =
    (mode === 'remove'
      ? trimmedServer !== ''
      : addKind === 'server'
      ? trimmedServer !== ''
      : trimmedDomain !== '') &&
    selected.size > 0 &&
    !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : resetAndClose}
      title="Bulk edit DNS"
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

        {mode === 'add' && (
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="dns-bulk-add-kind"
                checked={addKind === 'server'}
                onChange={() => setAddKind('server')}
                className="accent-info"
              />
              DNS server
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="dns-bulk-add-kind"
                checked={addKind === 'domain'}
                onChange={() => setAddKind('domain')}
                className="accent-info"
              />
              Domain-name
            </label>
          </div>
        )}

        {mode === 'remove' || addKind === 'server' ? (
          <FieldRow label="Server IP">
            <input
              type="text"
              value={server}
              onChange={(e) => setServer(e.target.value)}
              placeholder="e.g. 8.8.8.8"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
            <FieldError message={fieldErrors?.server} />
          </FieldRow>
        ) : (
          <FieldRow label="Domain">
            <input
              type="text"
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              placeholder="e.g. example.com"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
            <FieldError message={fieldErrors?.domain_name} />
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
