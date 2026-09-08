'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  addGlobalConfigDns,
  removeGlobalConfigDns,
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

type AddMode = 'server' | 'domain';

interface Props {
  open: boolean;
  onClose: () => void;
  deviceName: string;
  currentServers: string[] | null;
}

// DNS add takes EITHER a `server` OR a `domain_name` (backend validator:
// exactly one) -- surfaced here as a mode toggle. Remove only handles
// servers (there is no "clear domain_name" on the backend). Domain name
// itself doesn't come back on GET, so we don't try to show a current value
// for it; server list is the only piece with a read shape.
export function DnsEditModal({
  open,
  onClose,
  deviceName,
  currentServers,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [servers, setServers] = useState<string[]>([]);
  const [mode, setMode] = useState<AddMode>('server');
  const [serverDraft, setServerDraft] = useState('');
  const [domainDraft, setDomainDraft] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string> | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setServers(currentServers ?? []);
    setMode('server');
    setServerDraft('');
    setDomainDraft('');
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

  async function handleAddServer() {
    const s = serverDraft.trim();
    if (!s) return;
    if (servers.includes(s)) {
      setServerDraft('');
      return;
    }
    setPending('add-server');
    setError(null);
    setFieldErrors(null);
    try {
      const result = await addGlobalConfigDns(deviceName, { server: s });
      trackGroupJob(result.group_job_id, `Add DNS ${s} on ${deviceName}`);
      setServers([...servers, s]);
      setServerDraft('');
      invalidate();
    } catch (err) {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Add DNS server failed.'));
    } finally {
      setPending(null);
    }
  }

  async function handleSetDomain() {
    const d = domainDraft.trim();
    if (!d) return;
    setPending('set-domain');
    setError(null);
    setFieldErrors(null);
    try {
      const result = await addGlobalConfigDns(deviceName, { domain_name: d });
      trackGroupJob(result.group_job_id, `Set domain ${d} on ${deviceName}`);
      setDomainDraft('');
      invalidate();
    } catch (err) {
      const fields = parseFieldErrors(err);
      setFieldErrors(fields);
      setError(fields ? null : extractMessage(err, 'Set domain-name failed.'));
    } finally {
      setPending(null);
    }
  }

  async function handleRemove(s: string) {
    setPending(`remove:${s}`);
    setError(null);
    try {
      const result = await removeGlobalConfigDns(deviceName, { server: s });
      trackGroupJob(result.group_job_id, `Remove DNS ${s} on ${deviceName}`);
      setServers(servers.filter((x) => x !== s));
      invalidate();
    } catch (err) {
      setError(extractMessage(err, 'Remove DNS server failed.'));
    } finally {
      setPending(null);
    }
  }

  const busy = pending !== null;

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      title={`Edit DNS — ${deviceName}`}
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
        </section>

        <section className="flex flex-col gap-3 border-t border-panel-border pt-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-muted">
            Add
          </div>
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="dns-add-mode"
                checked={mode === 'server'}
                onChange={() => setMode('server')}
                className="accent-info"
              />
              DNS server
            </label>
            <label className="flex items-center gap-2 cursor-pointer">
              <input
                type="radio"
                name="dns-add-mode"
                checked={mode === 'domain'}
                onChange={() => setMode('domain')}
                className="accent-info"
              />
              Domain-name
            </label>
          </div>

          {mode === 'server' ? (
            <FieldRow label="Server IP">
              <input
                type="text"
                value={serverDraft}
                onChange={(e) => setServerDraft(e.target.value)}
                placeholder="e.g. 8.8.8.8"
                className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              />
              <FieldError message={fieldErrors?.server} />
            </FieldRow>
          ) : (
            <FieldRow label="Domain">
              <input
                type="text"
                value={domainDraft}
                onChange={(e) => setDomainDraft(e.target.value)}
                placeholder="e.g. example.com"
                className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
              />
              <FieldError message={fieldErrors?.domain_name} />
            </FieldRow>
          )}

          <div>
            {mode === 'server' ? (
              <ModalPrimary
                onClick={handleAddServer}
                disabled={busy || serverDraft.trim() === ''}
              >
                {pending === 'add-server' ? 'Adding…' : 'Add'}
              </ModalPrimary>
            ) : (
              <ModalPrimary
                onClick={handleSetDomain}
                disabled={busy || domainDraft.trim() === ''}
              >
                {pending === 'set-domain' ? 'Applying…' : 'Apply'}
              </ModalPrimary>
            )}
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
