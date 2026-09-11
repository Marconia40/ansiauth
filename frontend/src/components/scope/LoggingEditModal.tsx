'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  addGlobalConfigLogServer,
  removeGlobalConfigLogServer,
  parseFieldErrors,
} from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Modal } from './Modal';
import {
  FieldRow,
  FieldError,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';

interface Props {
  open: boolean;
  onClose: () => void;
  deviceName: string;
  currentServers: string[] | null;
  currentLevel: string | null;
}

// Log servers are incremental (POST / DELETE 1 at a time). `level` is a
// device-global setting -- not per-server -- but travels on the same
// endpoint by convenience per the backend schema, so we surface it as an
// optional field next to the server input.
export function LoggingEditModal({
  open,
  onClose,
  deviceName,
  currentServers,
  currentLevel,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [servers, setServers] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [levelDraft, setLevelDraft] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setServers(currentServers ?? []);
    setDraft('');
    setLevelDraft('');
    setPending(null);
    setError(null);
    setFieldErrors(null);
  }, [open, currentServers]);
  /* eslint-enable react-hooks/set-state-in-effect */

  function invalidate() {
    queryClient.invalidateQueries({
      queryKey: ['global-config', 'synced', deviceName],
    });
  }

  async function handleAdd() {
    const s = draft.trim();
    if (!s) return;
    const level = levelDraft.trim();
    if (servers.includes(s) && level === '') {
      setDraft('');
      return;
    }
    setPending('add');
    setError(null);
    setFieldErrors(null);
    try {
      const result = await addGlobalConfigLogServer(deviceName, {
        server: s,
        ...(level !== '' ? { level } : {}),
      });
      const label = level !== ''
        ? `Add log ${s} (level ${level}) on ${deviceName}`
        : `Add log ${s} on ${deviceName}`;
      trackGroupJob(result.group_job_id, label);
      if (!servers.includes(s)) setServers([...servers, s]);
      setDraft('');
      setLevelDraft('');
      invalidate();
    } catch (err) {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Add log server failed.'));
    } finally {
      setPending(null);
    }
  }

  async function handleRemove(s: string) {
    setPending(`remove:${s}`);
    setError(null);
    try {
      const result = await removeGlobalConfigLogServer(deviceName, { server: s });
      trackGroupJob(result.group_job_id, `Remove log ${s} on ${deviceName}`);
      setServers(servers.filter((x) => x !== s));
      invalidate();
    } catch (err) {
      setError(extractMessage(err, 'Remove log server failed.'));
    } finally {
      setPending(null);
    }
  }

  const busy = pending !== null;

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      title={`Edit logging — ${deviceName}`}
      footer={
        <ModalSecondary onClick={onClose} disabled={busy}>
          Close
        </ModalSecondary>
      }
    >
      <div className="flex flex-col gap-4">
        <section>
          <div className="text-xs font-semibold uppercase tracking-wider text-muted mb-2">
            Configured servers
          </div>
          {servers.length === 0 ? (
            <p className="text-sm italic text-muted">None configured.</p>
          ) : (
            <ul className="flex flex-col gap-1 text-sm font-mono">
              {servers.map((s) => (
                <li
                  key={s}
                  className="flex items-center justify-between gap-2 rounded bg-panel-elev/60 px-3 py-1.5"
                >
                  <span className="text-text">{s}</span>
                  <button
                    type="button"
                    onClick={() => handleRemove(s)}
                    disabled={busy}
                    aria-label={`Remove ${s}`}
                    className="text-danger hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed text-lg leading-none"
                  >
                    {pending === `remove:${s}` ? '…' : '×'}
                  </button>
                </li>
              ))}
            </ul>
          )}
          {currentLevel && (
            <p className="text-xs text-muted mt-2">
              Current device-wide log level:{' '}
              <span className="font-mono text-text">{currentLevel}</span>
            </p>
          )}
        </section>

        <section className="flex flex-col gap-3 border-t border-panel-border pt-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted">
            Add server
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <FieldRow label="Server IP">
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="e.g. 10.0.0.10"
                className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              />
              <FieldError message={fieldErrors?.server} />
            </FieldRow>
            <FieldRow label="Level (optional, device-wide)">
              <input
                type="text"
                value={levelDraft}
                onChange={(e) => setLevelDraft(e.target.value)}
                placeholder="e.g. informational"
                className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              />
              <FieldError message={fieldErrors?.level} />
            </FieldRow>
          </div>
          <p className="text-xs text-muted">
            Setting a level applies globally to the device, not just this
            server -- the field lives on the add endpoint by convenience.
          </p>
          <div>
            <ModalPrimary
              onClick={handleAdd}
              disabled={busy || draft.trim() === ''}
            >
              {pending === 'add' ? 'Adding…' : 'Add'}
            </ModalPrimary>
          </div>
        </section>

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
