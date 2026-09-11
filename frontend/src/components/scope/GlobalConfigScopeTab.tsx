'use client';

import { useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
  refreshDashboardScope,
  removeGlobalConfigRoute,
  deleteGlobalConfigAcl,
} from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { Scope } from './ScopeDashboard';
import { scopeToSummaryParams } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import { extractMessage } from './VlanCreateModal';
import {
  useScopeGlobalConfig,
  toSnmpRows,
  toNtpRows,
  toDnsRows,
  toLogRows,
  toRouteRows,
  toAclMatrix,
  type ServerRow,
  type RouteRow,
} from './scopeGlobalConfig';
import { SnmpBulkEditModal } from './SnmpBulkEditModal';
import { NtpBulkEditModal } from './NtpBulkEditModal';
import { DnsBulkEditModal } from './DnsBulkEditModal';
import { LogBulkEditModal } from './LogBulkEditModal';
import { RouteAddModal } from './RouteAddModal';
import { AclCreateOrAddModal } from './AclCreateOrAddModal';
import { AclRemoveRulesModal } from './AclRemoveRulesModal';

interface Props {
  scope: Scope;
}

type Section = 'snmp' | 'routes' | 'ntp' | 'dns' | 'logs' | 'acls';

interface SectionDef {
  key: Section;
  label: string;
}

const SECTIONS: SectionDef[] = [
  { key: 'snmp', label: 'SNMP' },
  { key: 'routes', label: 'Routes' },
  { key: 'ntp', label: 'NTP servers' },
  { key: 'dns', label: 'DNS servers' },
  { key: 'logs', label: 'Log servers' },
  { key: 'acls', label: 'ACL' },
];

type AclModalState =
  | { kind: 'closed' }
  | { kind: 'create'; deviceName: string; lockedName: string | null }
  | { kind: 'addRules'; deviceName: string; aclName: string }
  | { kind: 'removeRules'; deviceName: string; aclName: string; rules: string[] };

// Cross-device Global Config: same 6 sub-tabs as the device-scope
// GlobalConfigTab.tsx (SNMP/Routes/NTP/DNS/Log servers/ACL) minus
// ARP-MAC/Logs/Running-config, which stay device-only -- they don't make
// sense merged across a scope.
//
// SNMP/NTP/DNS/Log servers keep the per-device listing plus a bulk Edit
// button (select N devices, add/remove one value across all of them, 1 job
// per device -- see Snmp/Ntp/Dns/LogBulkEditModal.tsx). Routes and ACL stay
// single-device per edit action by design -- their "Edit"/action affordances
// reuse the EXISTING single-device modals (RouteAddModal,
// AclCreateOrAddModal, AclRemoveRulesModal) unchanged, just scoped to
// whichever device/ACL the user clicked. No backend changes anywhere: every
// write here is an existing per-device endpoint that already returns its
// own group_job_id.
//
// Unlike the Dashboard tab (ScopeDashboard.tsx), this tab does NOT
// auto-refresh on mount -- global_config sync is on-demand by design (it
// drags full running-configs, see backend/app/tasks.py "Decisión 3"). The
// Refresh button here opts into it explicitly via includeGlobalConfig.
export function GlobalConfigScopeTab({ scope }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();
  const { entries, isLoading, isError } = useScopeGlobalConfig(scope);

  const [section, setSection] = useState<Section>('snmp');
  const [refreshing, setRefreshing] = useState(false);
  const [expandedAcls, setExpandedAcls] = useState<Set<string>>(new Set());

  const snmpRows = useMemo(() => toSnmpRows(entries), [entries]);
  const ntpRows = useMemo(() => toNtpRows(entries), [entries]);
  const dnsRows = useMemo(() => toDnsRows(entries), [entries]);
  const logRows = useMemo(() => toLogRows(entries), [entries]);
  const routeRows = useMemo(() => toRouteRows(entries), [entries]);
  const aclMatrix = useMemo(() => toAclMatrix(entries), [entries]);
  const devices = useMemo(() => entries.map((e) => e.device).sort(), [entries]);

  const [openSnmpBulk, setOpenSnmpBulk] = useState(false);
  const [openNtpBulk, setOpenNtpBulk] = useState(false);
  const [openDnsBulk, setOpenDnsBulk] = useState(false);
  const [openLogBulk, setOpenLogBulk] = useState(false);

  const [routeAddDevice, setRouteAddDevice] = useState<string | null>(null);
  const [routeDeletingKey, setRouteDeletingKey] = useState<string | null>(null);
  const [routeError, setRouteError] = useState<string | null>(null);

  const [aclModal, setAclModal] = useState<AclModalState>({ kind: 'closed' });
  const [aclCreateDevice, setAclCreateDevice] = useState('');
  const [aclDeletingKey, setAclDeletingKey] = useState<string | null>(null);
  const [aclError, setAclError] = useState<string | null>(null);

  function invalidateScope() {
    queryClient.invalidateQueries({ queryKey: ['dashboard', 'summary'] });
  }

  async function handleRefresh() {
    setRefreshing(true);
    try {
      await refreshDashboardScope({ ...scopeToSummaryParams(scope), includeGlobalConfig: true });
    } finally {
      setRefreshing(false);
    }
  }

  function toggleAcl(key: string) {
    setExpandedAcls((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  async function handleDeleteRoute(device: string, destination: string, nextHop: string) {
    const key = `${device}|${destination}|${nextHop}`;
    setRouteDeletingKey(key);
    try {
      const result = await removeGlobalConfigRoute(device, {
        destination,
        next_hop: nextHop,
      });
      trackGroupJob(result.group_job_id, `Remove route ${destination} → ${nextHop} on ${device}`);
      invalidateScope();
    } catch (err) {
      setRouteError(extractMessage(err, 'Remove route failed.'));
      setTimeout(() => setRouteError(null), 4500);
    } finally {
      setRouteDeletingKey(null);
    }
  }

  async function handleDeleteAcl(device: string, name: string) {
    if (!window.confirm(`Delete ACL ${name} from ${device}?`)) return;
    const key = `${device}:${name}`;
    setAclDeletingKey(key);
    try {
      const result = await deleteGlobalConfigAcl(device, { name });
      trackGroupJob(result.group_job_id, `Delete ACL ${name} on ${device}`);
      invalidateScope();
    } catch (err) {
      setAclError(extractMessage(err, 'Delete ACL failed.'));
      setTimeout(() => setAclError(null), 4500);
    } finally {
      setAclDeletingKey(null);
    }
  }

  const emptyScope = devices.length === 0 && !isLoading;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton onClick={handleRefresh} loading={refreshing} disabled={emptyScope} />
      </div>

      <SubTabStrip current={section} onSelect={setSection} />

      {isError && (
        <div className="rounded-md border border-danger/60 bg-danger/10 text-danger px-3 py-2 text-sm">
          Failed to load global config data.
        </div>
      )}

      {section === 'snmp' && (
        <Panel title="SNMP" actions={<EditPill label="Edit" onClick={() => setOpenSnmpBulk(true)} />}>
          <SnmpTable rows={snmpRows} isLoading={isLoading} />
        </Panel>
      )}

      {section === 'routes' && (
        <Panel title="Routes">
          <RouteScopeTable
            rows={routeRows}
            devices={devices}
            isLoading={isLoading}
            deletingKey={routeDeletingKey}
            onAdd={setRouteAddDevice}
            onDelete={handleDeleteRoute}
          />
          {routeError && (
            <p className="mt-2 text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
              {routeError}
            </p>
          )}
        </Panel>
      )}

      {section === 'ntp' && (
        <Panel title="NTP servers" actions={<EditPill label="Edit" onClick={() => setOpenNtpBulk(true)} />}>
          <ServerTable
            rows={ntpRows}
            devices={devices}
            isLoading={isLoading}
            emptyLabel="No NTP servers configured in this scope."
          />
        </Panel>
      )}

      {section === 'dns' && (
        <Panel title="DNS servers" actions={<EditPill label="Edit" onClick={() => setOpenDnsBulk(true)} />}>
          <ServerTable
            rows={dnsRows}
            devices={devices}
            isLoading={isLoading}
            emptyLabel="No DNS servers configured in this scope."
          />
        </Panel>
      )}

      {section === 'logs' && (
        <Panel title="Log servers" actions={<EditPill label="Edit" onClick={() => setOpenLogBulk(true)} />}>
          <ServerTable
            rows={logRows}
            devices={devices}
            isLoading={isLoading}
            showLevel
            emptyLabel="No log servers configured in this scope."
          />
        </Panel>
      )}

      {section === 'acls' && (
        <Panel
          title="ACLs"
          actions={
            <CreateAclControls
              devices={devices}
              value={aclCreateDevice}
              onChange={setAclCreateDevice}
              onCreate={() =>
                setAclModal({ kind: 'create', deviceName: aclCreateDevice, lockedName: null })
              }
            />
          }
        >
          <AclMatrixTable
            rows={aclMatrix}
            devices={devices}
            isLoading={isLoading}
            expanded={expandedAcls}
            onToggle={toggleAcl}
            onAddRules={(device, name) =>
              setAclModal({ kind: 'addRules', deviceName: device, aclName: name })
            }
            onRemoveRules={(device, name, rules) =>
              setAclModal({ kind: 'removeRules', deviceName: device, aclName: name, rules })
            }
            onDelete={handleDeleteAcl}
            onCreateForDevice={(device, name) =>
              setAclModal({ kind: 'create', deviceName: device, lockedName: name })
            }
            deletingKey={aclDeletingKey}
          />
          {aclError && (
            <p className="mt-2 text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
              {aclError}
            </p>
          )}
        </Panel>
      )}

      {/* SNMP/NTP/DNS/Log bulk modals already invalidate the scope query
          themselves on success (see each file) -- onClose here only needs
          to close. */}
      <SnmpBulkEditModal open={openSnmpBulk} onClose={() => setOpenSnmpBulk(false)} scope={scope} />
      <NtpBulkEditModal open={openNtpBulk} onClose={() => setOpenNtpBulk(false)} scope={scope} />
      <DnsBulkEditModal open={openDnsBulk} onClose={() => setOpenDnsBulk(false)} scope={scope} />
      <LogBulkEditModal open={openLogBulk} onClose={() => setOpenLogBulk(false)} scope={scope} />

      {/* Routes/ACL reuse the existing single-device modals unchanged --
          those only invalidate the device-scope query key, so we also
          invalidate the scope-level one here on close. */}
      {routeAddDevice && (
        <RouteAddModal
          open
          onClose={() => {
            setRouteAddDevice(null);
            invalidateScope();
          }}
          deviceName={routeAddDevice}
        />
      )}

      <AclCreateOrAddModal
        open={aclModal.kind === 'create'}
        onClose={() => {
          setAclModal({ kind: 'closed' });
          if (aclModal.kind === 'create') setAclCreateDevice('');
          invalidateScope();
        }}
        deviceName={aclModal.kind === 'create' ? aclModal.deviceName : ''}
        lockedName={aclModal.kind === 'create' ? aclModal.lockedName : null}
      />
      {aclModal.kind === 'addRules' && (
        <AclCreateOrAddModal
          open
          onClose={() => {
            setAclModal({ kind: 'closed' });
            invalidateScope();
          }}
          deviceName={aclModal.deviceName}
          lockedName={aclModal.aclName}
        />
      )}
      {aclModal.kind === 'removeRules' && (
        <AclRemoveRulesModal
          open
          onClose={() => {
            setAclModal({ kind: 'closed' });
            invalidateScope();
          }}
          deviceName={aclModal.deviceName}
          aclName={aclModal.aclName}
          currentRuleLines={aclModal.rules}
        />
      )}
    </div>
  );
}

function SubTabStrip({
  current,
  onSelect,
}: {
  current: Section;
  onSelect: (s: Section) => void;
}) {
  return (
    <div className="border-b border-panel-border flex items-end gap-1 -mb-px overflow-x-auto">
      {SECTIONS.map((s) => {
        const active = s.key === current;
        return (
          <button
            type="button"
            key={s.key}
            onClick={() => onSelect(s.key)}
            className={`px-4 py-2 text-xs font-semibold uppercase tracking-wider transition-colors border-b-2 ${
              active
                ? 'border-info text-text bg-panel/60'
                : 'border-transparent text-muted hover:text-text hover:bg-panel/40'
            }`}
          >
            {s.label}
          </button>
        );
      })}
    </div>
  );
}

function EditPill({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-xs font-semibold uppercase tracking-wider text-info hover:brightness-125 transition-colors"
    >
      {label}
    </button>
  );
}

function SnmpTable({
  rows,
  isLoading,
}: {
  rows: ReturnType<typeof toSnmpRows>;
  isLoading: boolean;
}) {
  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading SNMP data…</p>;
  }
  if (rows.length === 0) {
    return <p className="text-sm italic text-muted py-6">No devices in this scope.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border border-panel-border">
      <table className="w-full text-sm">
        <thead className="bg-panel-elev/60 text-muted uppercase text-xs tracking-wider">
          <tr>
            <th className="text-left font-semibold px-4 py-2">Device</th>
            <th className="text-left font-semibold px-4 py-2 w-20">Enabled</th>
            <th className="text-left font-semibold px-4 py-2">Version</th>
            <th className="text-left font-semibold px-4 py-2">Community</th>
            <th className="text-left font-semibold px-4 py-2 w-24">Permission</th>
            <th className="text-left font-semibold px-4 py-2">Trap hosts</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-panel-border">
          {rows.map((row) => (
            <tr key={row.device} className="hover:bg-panel-elev/40 transition-colors">
              <td className="px-4 py-2 text-text">{row.device}</td>
              <td className="px-4 py-2">
                {row.enabled === true ? (
                  <span className="text-success">Yes</span>
                ) : row.enabled === false ? (
                  <span className="text-muted">No</span>
                ) : (
                  <span className="text-muted italic">—</span>
                )}
              </td>
              <td className="px-4 py-2 text-text">{row.version || <Dash />}</td>
              <td className="px-4 py-2 text-text font-mono">{row.community || <Dash />}</td>
              <td className="px-4 py-2 text-text">{row.permission || <Dash />}</td>
              <td className="px-4 py-2 text-text">
                {row.trapHosts.length === 0 ? <Dash /> : row.trapHosts.join(', ')}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Grouped by device (one header per device, its servers listed under it) --
// same layout as RouteScopeTable below, so the device name isn't repeated
// on every row.
function ServerTable({
  rows,
  devices,
  isLoading,
  showLevel,
  emptyLabel,
}: {
  rows: ServerRow[];
  devices: string[];
  isLoading: boolean;
  showLevel?: boolean;
  emptyLabel: string;
}) {
  const byDevice = useMemo(() => {
    const map = new Map<string, ServerRow[]>();
    for (const d of devices) map.set(d, []);
    for (const r of rows) {
      const bucket = map.get(r.device) ?? [];
      bucket.push(r);
      map.set(r.device, bucket);
    }
    return map;
  }, [rows, devices]);

  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading…</p>;
  }
  if (devices.length === 0) {
    return <p className="text-sm italic text-muted py-6">{emptyLabel}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      {devices.map((device) => {
        const deviceServers = byDevice.get(device) ?? [];
        return (
          <div key={device} className="rounded-md border border-panel-border overflow-hidden">
            <div className="bg-panel-elev/60 px-4 py-2">
              <span className="text-sm font-semibold text-text">{device}</span>
            </div>
            {deviceServers.length === 0 ? (
              <p className="px-4 py-3 text-sm italic text-muted">None configured.</p>
            ) : (
              <table className="w-full text-sm">
                {showLevel && (
                  <thead className="text-xs uppercase tracking-wider text-muted">
                    <tr>
                      <th className="text-left font-medium px-4 py-1.5">Server</th>
                      <th className="text-left font-medium px-4 py-1.5 w-32">Level</th>
                    </tr>
                  </thead>
                )}
                <tbody className="divide-y divide-panel-border">
                  {deviceServers.map((row, i) => (
                    <tr key={`${row.server}:${i}`}>
                      <td className="px-4 py-1.5 text-text font-mono">{row.server}</td>
                      {showLevel && <td className="px-4 py-1.5 text-text">{row.level || <Dash />}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </div>
  );
}

function RouteScopeTable({
  rows,
  devices,
  isLoading,
  deletingKey,
  onAdd,
  onDelete,
}: {
  rows: RouteRow[];
  devices: string[];
  isLoading: boolean;
  deletingKey: string | null;
  onAdd: (device: string) => void;
  onDelete: (device: string, destination: string, nextHop: string) => void;
}) {
  const byDevice = useMemo(() => {
    const map = new Map<string, RouteRow[]>();
    for (const d of devices) map.set(d, []);
    for (const r of rows) {
      const bucket = map.get(r.device) ?? [];
      bucket.push(r);
      map.set(r.device, bucket);
    }
    return map;
  }, [rows, devices]);

  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading routes…</p>;
  }
  if (devices.length === 0) {
    return <p className="text-sm italic text-muted py-6">No devices in this scope.</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      {devices.map((device) => {
        const deviceRoutes = byDevice.get(device) ?? [];
        return (
          <div key={device} className="rounded-md border border-panel-border overflow-hidden">
            <div className="flex items-center justify-between gap-2 bg-panel-elev/60 px-4 py-2">
              <span className="text-sm font-semibold text-text">{device}</span>
              <button
                type="button"
                onClick={() => onAdd(device)}
                className="text-xs font-semibold uppercase tracking-wider text-info hover:brightness-125 transition-colors"
              >
                + Add route
              </button>
            </div>
            {deviceRoutes.length === 0 ? (
              <p className="px-4 py-3 text-sm italic text-muted">No static routes configured.</p>
            ) : (
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-muted">
                  <tr>
                    <th className="text-left font-medium px-4 py-1.5">Destination</th>
                    <th className="text-left font-medium px-4 py-1.5">Next-hop</th>
                    <th className="text-left font-medium px-4 py-1.5">Interface</th>
                    <th className="text-right font-medium px-4 py-1.5 w-16">Actions</th>
                  </tr>
                </thead>
                <tbody className="font-mono divide-y divide-panel-border">
                  {deviceRoutes.map((r, idx) => {
                    const canDelete = r.destination !== null && r.nextHop !== null;
                    const rowKey = `${device}|${r.destination}|${r.nextHop}`;
                    const isDeleting = deletingKey === rowKey;
                    return (
                      <tr key={`${rowKey}:${idx}`}>
                        <td className="px-4 py-1.5 text-text">{r.destination ?? '—'}</td>
                        <td className="px-4 py-1.5 text-text">{r.nextHop ?? '—'}</td>
                        <td className="px-4 py-1.5 text-text">{r.interface ?? '—'}</td>
                        <td className="px-4 py-1.5 text-right">
                          {canDelete ? (
                            <button
                              type="button"
                              onClick={() => onDelete(device, r.destination as string, r.nextHop as string)}
                              disabled={deletingKey !== null}
                              aria-label={`Remove ${r.destination} → ${r.nextHop}`}
                              className="text-danger hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed text-lg leading-none"
                            >
                              {isDeleting ? '…' : '×'}
                            </button>
                          ) : (
                            <span className="text-muted" title="Delete requires both destination and next-hop">
                              —
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </div>
  );
}

function CreateAclControls({
  devices,
  value,
  onChange,
  onCreate,
}: {
  devices: string[];
  value: string;
  onChange: (v: string) => void;
  onCreate: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-md bg-panel-elev border border-panel-border px-2 py-1 text-xs text-text focus:outline-none focus:ring-2 focus:ring-info"
      >
        <option value="">Choose device…</option>
        {devices.map((d) => (
          <option key={d} value={d}>
            {d}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={onCreate}
        disabled={value === ''}
        className="text-xs font-semibold uppercase tracking-wider text-info hover:brightness-125 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
      >
        + Create ACL
      </button>
    </div>
  );
}

function AclMatrixTable({
  rows,
  devices,
  isLoading,
  expanded,
  onToggle,
  onAddRules,
  onRemoveRules,
  onDelete,
  onCreateForDevice,
  deletingKey,
}: {
  rows: ReturnType<typeof toAclMatrix>;
  devices: string[];
  isLoading: boolean;
  expanded: Set<string>;
  onToggle: (key: string) => void;
  onAddRules: (device: string, name: string) => void;
  onRemoveRules: (device: string, name: string, rules: string[]) => void;
  onDelete: (device: string, name: string) => void;
  onCreateForDevice: (device: string, name: string) => void;
  deletingKey: string | null;
}) {
  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading ACL data…</p>;
  }
  if (rows.length === 0) {
    return <p className="text-sm italic text-muted py-6">No ACLs found in this scope.</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border border-panel-border">
      <table className="w-full text-sm">
        <thead className="bg-panel-elev/60 text-muted uppercase text-xs tracking-wider">
          <tr>
            <th className="text-left font-semibold px-4 py-2">ACL</th>
            {devices.map((d) => (
              <th key={d} className="text-left font-semibold px-4 py-2">
                {d}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-panel-border">
          {rows.map((row) => (
            <tr key={row.name} className="hover:bg-panel-elev/40 transition-colors align-top">
              <td className="px-4 py-2 font-mono font-semibold text-text">{row.name}</td>
              {devices.map((device) => {
                const cell = row.perDevice[device];
                const key = `${row.name}:${device}`;
                if (!cell) {
                  return (
                    <td key={device} className="px-4 py-2">
                      <button
                        type="button"
                        onClick={() => onCreateForDevice(device, row.name)}
                        className="text-xs text-info hover:brightness-125"
                      >
                        + Create
                      </button>
                    </td>
                  );
                }
                const isOpen = expanded.has(key);
                const deleteKey = `${device}:${row.name}`;
                const isDeleting = deletingKey === deleteKey;
                return (
                  <td key={device} className="px-4 py-2">
                    <button
                      type="button"
                      onClick={() => onToggle(key)}
                      className="text-left text-info hover:brightness-125 tabular-nums block"
                      title={cell.type ?? undefined}
                    >
                      {isOpen ? '▾' : '▸'} {cell.ruleCount} rule{cell.ruleCount === 1 ? '' : 's'}
                    </button>
                    {isOpen && (
                      <div className="flex flex-wrap gap-x-2 mt-1 text-[11px]">
                        <AclCellAction onClick={() => onAddRules(device, row.name)}>
                          Add rules
                        </AclCellAction>
                        <AclCellAction
                          onClick={() => onRemoveRules(device, row.name, cell.rules)}
                          disabled={cell.rules.length === 0}
                        >
                          Remove rules
                        </AclCellAction>
                        <AclCellAction
                          onClick={() => onDelete(device, row.name)}
                          disabled={isDeleting}
                          tone="danger"
                        >
                          {isDeleting ? 'Deleting…' : 'Delete'}
                        </AclCellAction>
                      </div>
                    )}
                    {isOpen && (
                      <ul className="mt-1 flex flex-col gap-0.5 text-xs font-mono text-muted">
                        {cell.rules.length === 0 ? (
                          <li className="italic">Empty ACL.</li>
                        ) : (
                          cell.rules.map((line, i) => <li key={i}>{line}</li>)
                        )}
                      </ul>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AclCellAction({
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
  const color = tone === 'danger' ? 'text-danger hover:brightness-125' : 'text-info hover:brightness-125';
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`font-semibold uppercase tracking-wider disabled:opacity-40 disabled:cursor-not-allowed transition-colors ${color}`}
    >
      {children}
    </button>
  );
}

function Dash() {
  return <span className="text-muted italic">—</span>;
}
