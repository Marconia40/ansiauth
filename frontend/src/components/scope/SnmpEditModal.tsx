'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { setGlobalConfigSnmp } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type {
  GlobalConfigSnmpInfo,
  SnmpUpdateRequest,
} from '@/types/global-config';
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
  /** Snapshot of the cached SNMP block — used only as initial values. */
  currentSnmp: GlobalConfigSnmpInfo | null;
}

// PATCH /devices/{name}/global-config/snmp (RF-GLOBAL-07). Community is
// always applied read-only server-side (there is no permission input any
// more), trap_source has no confirmed effect on Huawei yet, and trap_host
// must be sent together with trap_version -- mirror those constraints in
// the form so the request never gets rejected 422 by the backend
// validator.
export function SnmpEditModal({
  open,
  onClose,
  deviceName,
  currentSnmp,
}: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [version, setVersion] = useState('');
  const [community, setCommunity] = useState('');
  const [trapSource, setTrapSource] = useState('');
  const [trapHost, setTrapHost] = useState('');
  const [trapVersion, setTrapVersion] = useState('');
  const [error, setError] = useState<string | null>(null);

  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setVersion(currentSnmp?.version ?? '');
    setCommunity(currentSnmp?.community ?? '');
    setTrapSource('');
    // trap_hosts is a list (there can be more than 1) and the write path
    // targets a single host; pre-filling from a random index would just be
    // confusing, so we always start blank.
    setTrapHost('');
    setTrapVersion('');
    setError(null);
  }, [open, currentSnmp]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const trimmedVersion = version.trim();
  const trimmedCommunity = community.trim();
  const trimmedTrapSource = trapSource.trim();
  const trimmedTrapHost = trapHost.trim();
  const trimmedTrapVersion = trapVersion.trim();

  // trap_host and trap_version must be provided together.
  const trapPairMismatch =
    (trimmedTrapHost === '') !== (trimmedTrapVersion === '');
  const trapPairComplete =
    trimmedTrapHost !== '' && trimmedTrapVersion !== '';

  // At least one of the 4 knobs must be set for the request to be valid.
  const anySet =
    trimmedVersion !== '' ||
    trimmedCommunity !== '' ||
    trimmedTrapSource !== '' ||
    trapPairComplete;

  const body: SnmpUpdateRequest = useMemo(() => {
    const out: SnmpUpdateRequest = {};
    if (trimmedVersion !== '') out.version = trimmedVersion;
    if (trimmedCommunity !== '') out.community = trimmedCommunity;
    if (trimmedTrapSource !== '') out.trap_source = trimmedTrapSource;
    if (trapPairComplete) {
      out.trap_host = trimmedTrapHost;
      out.trap_version = trimmedTrapVersion;
    }
    return out;
  }, [
    trimmedVersion,
    trimmedCommunity,
    trimmedTrapSource,
    trapPairComplete,
    trimmedTrapHost,
    trimmedTrapVersion,
  ]);

  const mutation = useMutation({
    mutationFn: () => setGlobalConfigSnmp(deviceName, body),
    onSuccess: (result) => {
      trackGroupJob(result.group_job_id, `Update SNMP on ${deviceName}`);
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      onClose();
    },
    onError: (err: unknown) => {
      setError(extractMessage(err, 'Failed to queue the SNMP change.'));
    },
  });

  const canSubmit =
    anySet && !trapPairMismatch && !mutation.isPending;

  return (
    <Modal
      open={open}
      onClose={mutation.isPending ? () => undefined : onClose}
      title={`Edit SNMP — ${deviceName}`}
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
        <p className="text-xs text-muted">
          Community is applied as read-only. Leave a field blank to leave it
          untouched — at least one knob must be filled.
        </p>

        <FieldRow label="Version">
          <input
            type="text"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
            placeholder="e.g. v2c (VRP only)"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>

        <FieldRow label="Community">
          <input
            type="text"
            value={community}
            onChange={(e) => setCommunity(e.target.value)}
            placeholder="e.g. public"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>

        <FieldRow label="Trap source interface">
          <input
            type="text"
            value={trapSource}
            onChange={(e) => setTrapSource(e.target.value)}
            placeholder="e.g. Loopback0"
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <FieldRow label="Trap host">
            <input
              type="text"
              value={trapHost}
              onChange={(e) => setTrapHost(e.target.value)}
              placeholder="e.g. 10.0.0.5"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
          </FieldRow>
          <FieldRow label="Trap version">
            <input
              type="text"
              value={trapVersion}
              onChange={(e) => setTrapVersion(e.target.value)}
              placeholder="e.g. 2c"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
          </FieldRow>
        </div>

        {trapPairMismatch && (
          <p className="text-xs text-danger">
            Trap host and trap version must be provided together.
          </p>
        )}
        {!anySet && (
          <p className="text-xs text-muted">
            Fill at least one field to enable Apply.
          </p>
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
