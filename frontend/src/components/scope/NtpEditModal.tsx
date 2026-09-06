'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  addGlobalConfigNtp,
  removeGlobalConfigNtp,
} from '@/services/api';
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
  currentServers: string[] | null;
  onDone?: (msg: string, tone: 'ok' | 'error') => void;
}

// NTP servers are incremental (POST / DELETE 1 server at a time) so this
// modal fires each mutation immediately -- same pattern as SVI DHCP relay.
// `prefer` is Cisco-only per the backend schema; on Huawei the flag has no
// confirmed effect but the API accepts it, so we send it as-is.
export function NtpEditModal({
  open,
  onClose,
  deviceName,
  currentServers,
  onDone,
}: Props) {
  const queryClient = useQueryClient();
  const { trackJob, trackGroupJob } = useJobNotifications();

  const [servers, setServers] = useState<string[]>([]);
  const [draft, setDraft] = useState('');
  const [prefer, setPrefer] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setServers(currentServers ?? []);
    setDraft('');
    setPrefer(false);
    setPending(null);
    setError(null);
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
    if (servers.includes(s)) {
      setDraft('');
      return;
    }
    setPending('add');
    setError(null);
    try {
      const result = await addGlobalConfigNtp(deviceName, {
        server: s,
        ...(prefer ? { prefer: true } : {}),
      });
      trackGroupJob(result.group_job_id, `Add NTP ${s} on ${deviceName}`);
      for (const j of result.jobs) {
        trackJob(j.job_id, `Add NTP ${s} on ${deviceName}`, j.device);
      }
      setServers([...servers, s]);
      setDraft('');
      setPrefer(false);
      invalidate();
      onDone?.(`Add NTP ${s} queued on ${deviceName}.`, 'ok');
    } catch (err) {
      setError(extractMessage(err, 'Add NTP server failed.'));
    } finally {
      setPending(null);
    }
  }

  async function handleRemove(s: string) {
    setPending(`remove:${s}`);
    setError(null);
    try {
      const result = await removeGlobalConfigNtp(deviceName, { server: s });
      trackGroupJob(result.group_job_id, `Remove NTP ${s} on ${deviceName}`);
      for (const j of result.jobs) {
        trackJob(j.job_id, `Remove NTP ${s} on ${deviceName}`, j.device);
      }
      setServers(servers.filter((x) => x !== s));
      invalidate();
      onDone?.(`Remove NTP ${s} queued on ${deviceName}.`, 'ok');
    } catch (err) {
      setError(extractMessage(err, 'Remove NTP server failed.'));
    } finally {
      setPending(null);
    }
  }

  const busy = pending !== null;

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      title={`Edit NTP servers — ${deviceName}`}
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
            Add server
          </div>
          <FieldRow label="Server IP">
            <input
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="e.g. 10.0.0.5"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
          </FieldRow>
          <label className="flex items-center gap-2 text-sm text-text">
            <input
              type="checkbox"
              checked={prefer}
              onChange={(e) => setPrefer(e.target.checked)}
              className="accent-info"
            />
            Mark as preferred (Cisco; no confirmed effect on Huawei)
          </label>
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
