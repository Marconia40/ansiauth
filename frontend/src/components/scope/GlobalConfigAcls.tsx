'use client';

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteGlobalConfigAcl,
  getGlobalConfigSynced,
  refreshDeviceGlobalConfig,
  type SyncedResource,
} from '@/services/api';
import type {
  GlobalConfigAclInfo,
  GlobalConfigRead,
} from '@/types/global-config';
import { useJobNotifications } from '@/context/JobNotificationContext';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { SYNC_POLL_INTERVAL_MS } from '@/lib/syncPolling';
import { AclCreateOrAddModal } from './AclCreateOrAddModal';
import { AclRemoveRulesModal } from './AclRemoveRulesModal';
import { extractMessage } from './VlanCreateModal';

interface Props {
  deviceName: string;
}

type ModalState =
  | { kind: 'closed' }
  | { kind: 'create' }
  | { kind: 'addRules'; name: string }
  | { kind: 'removeRules'; name: string; rules: string[] };

// ACLs sub-tab. Reuses the same query key as the Overview (single
// `global_config` sync scope). Rows are collapsed by default; expand to
// see raw rule lines + per-ACL actions (Add rules / Remove rules /
// Delete ACL). The "Create ACL" button on top opens the same modal with
// no locked name.
export function GlobalConfigAcls({ deviceName }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const configQuery = useQuery({
    queryKey: ['global-config', 'synced', deviceName],
    queryFn: () => getGlobalConfigSynced(deviceName),
    enabled: Boolean(deviceName),
    refetchInterval: (query: {
      state: { data?: SyncedResource<GlobalConfigRead> };
    }) => (query.state.data?.sync_in_progress ? SYNC_POLL_INTERVAL_MS : false),
  });

  const inProgress = Boolean(configQuery.data?.sync_in_progress);
  const syncedAt = configQuery.data?.synced_at ?? null;
  const syncError = configQuery.data?.sync_error ?? null;
  const acls = configQuery.data?.data.acls ?? null;

  const [refreshing, setRefreshing] = useState(false);
  const [modal, setModal] = useState<ModalState>({ kind: 'closed' });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [deletingName, setDeletingName] = useState<string | null>(null);
  // Only used for delete errors -- a success hands off to
  // JobNotificationContext's tracked toast (trackGroupJob below).
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDeviceGlobalConfig(deviceName);
    } finally {
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'version', 'synced', deviceName],
      });
      setRefreshing(false);
    }
  }

  function toggleExpand(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  async function handleDelete(name: string) {
    if (!window.confirm(`Delete ACL ${name} from ${deviceName}?`)) return;
    setDeletingName(name);
    try {
      const result = await deleteGlobalConfigAcl(deviceName, { name });
      trackGroupJob(result.group_job_id, `Delete ACL ${name} on ${deviceName}`);
      queryClient.invalidateQueries({
        queryKey: ['global-config', 'synced', deviceName],
      });
    } catch (err) {
      setDeleteError(extractMessage(err, 'Delete ACL failed.'));
      setTimeout(() => setDeleteError(null), 4500);
    } finally {
      setDeletingName(null);
    }
  }

  const loading = configQuery.isLoading;
  const neverSynced =
    !loading && !inProgress && !syncError && syncedAt === null && acls === null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={() => setModal({ kind: 'create' })}
          className="rounded-md px-4 py-2 text-sm font-semibold uppercase tracking-wider border border-panel-border text-text hover:bg-panel-elev transition-colors"
        >
          Create ACL
        </button>
        <RefreshButton
          onClick={handleRefresh}
          loading={refreshing || inProgress}
          syncedAt={syncedAt}
          syncError={syncError}
        />
      </div>

      {syncError && (
        <div className="rounded-md border border-danger/60 bg-danger/10 text-danger px-3 py-2 text-sm">
          Last sync failed: {syncError}
        </div>
      )}

      {neverSynced && (
        <div className="rounded-md border border-panel-border bg-panel-elev/40 text-muted px-3 py-2 text-sm italic">
          No global-config data cached yet — click Refresh to pull it from the device.
        </div>
      )}

      <Panel title="ACLs">
        <AclsList
          acls={acls}
          loading={loading}
          expanded={expanded}
          onToggle={toggleExpand}
          onAddRules={(name) => setModal({ kind: 'addRules', name })}
          onRemoveRules={(name, rules) =>
            setModal({ kind: 'removeRules', name, rules })
          }
          onDelete={handleDelete}
          deletingName={deletingName}
        />
      </Panel>

      <AclCreateOrAddModal
        open={modal.kind === 'create' || modal.kind === 'addRules'}
        onClose={() => setModal({ kind: 'closed' })}
        deviceName={deviceName}
        lockedName={modal.kind === 'addRules' ? modal.name : null}
      />
      {modal.kind === 'removeRules' && (
        <AclRemoveRulesModal
          open
          onClose={() => setModal({ kind: 'closed' })}
          deviceName={deviceName}
          aclName={modal.name}
          currentRuleLines={modal.rules}
        />
      )}

      {deleteError && (
        <div className="fixed bottom-4 right-4 z-40 rounded-md px-4 py-2 shadow-lg text-sm bg-danger text-white">
          {deleteError}
        </div>
      )}
    </div>
  );
}

function AclsList({
  acls,
  loading,
  expanded,
  onToggle,
  onAddRules,
  onRemoveRules,
  onDelete,
  deletingName,
}: {
  acls: GlobalConfigAclInfo[] | null;
  loading: boolean;
  expanded: Set<string>;
  onToggle: (name: string) => void;
  onAddRules: (name: string) => void;
  onRemoveRules: (name: string, rules: string[]) => void;
  onDelete: (name: string) => void;
  deletingName: string | null;
}) {
  if (loading) {
    return <p className="text-sm italic text-muted">Loading…</p>;
  }
  if (acls === null) {
    return <p className="text-sm italic text-muted">No ACL data cached yet.</p>;
  }
  if (acls.length === 0) {
    return <p className="text-sm italic text-muted">No ACLs configured.</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {acls.map((acl) => {
        const isOpen = expanded.has(acl.name);
        const isDeleting = deletingName === acl.name;
        return (
          <li
            key={acl.name}
            className="rounded-md border border-panel-border bg-panel-elev/40 overflow-hidden"
          >
            <div className="flex items-center gap-3 px-3 py-2">
              <button
                type="button"
                onClick={() => onToggle(acl.name)}
                aria-label={isOpen ? 'Collapse' : 'Expand'}
                className="text-info text-lg leading-none w-6"
              >
                {isOpen ? '▾' : '▸'}
              </button>
              <span className="font-mono font-semibold text-text">{acl.name}</span>
              <span className="text-xs text-muted">{acl.type ?? '—'}</span>
              <span className="text-xs text-muted ml-auto tabular-nums">
                {acl.rules.length} rule{acl.rules.length === 1 ? '' : 's'}
              </span>
              <div className="flex items-center gap-1">
                <ActionLink onClick={() => onAddRules(acl.name)}>
                  Add rules
                </ActionLink>
                <ActionLink
                  onClick={() => onRemoveRules(acl.name, acl.rules)}
                  disabled={acl.rules.length === 0}
                >
                  Remove rules
                </ActionLink>
                <ActionLink
                  onClick={() => onDelete(acl.name)}
                  disabled={isDeleting}
                  tone="danger"
                >
                  {isDeleting ? 'Deleting…' : 'Delete ACL'}
                </ActionLink>
              </div>
            </div>
            {isOpen && (
              <div className="border-t border-panel-border bg-panel/60 px-3 py-2">
                {acl.rules.length === 0 ? (
                  <p className="text-xs italic text-muted">Empty ACL.</p>
                ) : (
                  <ul className="flex flex-col gap-0.5 text-xs font-mono">
                    {acl.rules.map((line, i) => (
                      <li key={i} className="text-text">
                        {line}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ActionLink({
  onClick,
  disabled,
  tone = 'default',
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  tone?: 'default' | 'danger';
  children: React.ReactNode;
}) {
  const color =
    tone === 'danger'
      ? 'text-danger hover:brightness-125'
      : 'text-info hover:brightness-125';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`text-xs font-semibold uppercase tracking-wider px-2 py-1 rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${color}`}
    >
      {children}
    </button>
  );
}
