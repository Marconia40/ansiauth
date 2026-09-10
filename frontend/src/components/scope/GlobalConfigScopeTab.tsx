'use client';

import { useMemo, useState } from 'react';
import { refreshDashboardScope } from '@/services/api';
import type { Scope } from './ScopeDashboard';
import { scopeToSummaryParams } from './ScopeDashboard';
import { Panel } from './Panel';
import { RefreshButton } from './RefreshButton';
import {
  useScopeGlobalConfig,
  toSnmpRows,
  toNtpRows,
  toDnsRows,
  toLogRows,
  toAclMatrix,
  type ServerRow,
} from './scopeGlobalConfig';

interface Props {
  scope: Scope;
}

// Cross-device Global Config: SNMP/NTP/DNS/Log servers as per-device lists,
// plus a read-only ACL comparison matrix (which device has which ACL).
// ARP/MAC, Logs and Running-config stay device-only (GlobalConfigTab.tsx) --
// they don't make sense merged across a scope. Deploying/copying an ACL to
// fill a gap is explicitly out of scope for this pass.
//
// Unlike the Dashboard tab (ScopeDashboard.tsx), this tab does NOT
// auto-refresh on mount -- global_config sync is on-demand by design (it
// drags full running-configs, see backend/app/tasks.py "Decisión 3"). The
// Refresh button here opts into it explicitly via includeGlobalConfig.
export function GlobalConfigScopeTab({ scope }: Props) {
  const { entries, isLoading, isError } = useScopeGlobalConfig(scope);
  const [refreshing, setRefreshing] = useState(false);
  const [expandedAcls, setExpandedAcls] = useState<Set<string>>(new Set());

  const snmpRows = useMemo(() => toSnmpRows(entries), [entries]);
  const ntpRows = useMemo(() => toNtpRows(entries), [entries]);
  const dnsRows = useMemo(() => toDnsRows(entries), [entries]);
  const logRows = useMemo(() => toLogRows(entries), [entries]);
  const aclMatrix = useMemo(() => toAclMatrix(entries), [entries]);
  const devices = useMemo(() => entries.map((e) => e.device).sort(), [entries]);

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

  const emptyScope = devices.length === 0 && !isLoading;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-end">
        <RefreshButton onClick={handleRefresh} loading={refreshing} disabled={emptyScope} />
      </div>

      {isError && (
        <div className="rounded-md border border-danger/60 bg-danger/10 text-danger px-3 py-2 text-sm">
          Failed to load global config data.
        </div>
      )}

      <Panel title="SNMP">
        <SnmpTable rows={snmpRows} isLoading={isLoading} />
      </Panel>

      <Panel title="NTP servers">
        <ServerTable rows={ntpRows} isLoading={isLoading} emptyLabel="No NTP servers configured in this scope." />
      </Panel>

      <Panel title="DNS servers">
        <ServerTable rows={dnsRows} isLoading={isLoading} emptyLabel="No DNS servers configured in this scope." />
      </Panel>

      <Panel title="Log servers">
        <ServerTable
          rows={logRows}
          isLoading={isLoading}
          showLevel
          emptyLabel="No log servers configured in this scope."
        />
      </Panel>

      <Panel title="ACLs">
        <AclMatrixTable
          rows={aclMatrix}
          devices={devices}
          isLoading={isLoading}
          expanded={expandedAcls}
          onToggle={toggleAcl}
        />
      </Panel>
    </div>
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

function ServerTable({
  rows,
  isLoading,
  showLevel,
  emptyLabel,
}: {
  rows: ServerRow[];
  isLoading: boolean;
  showLevel?: boolean;
  emptyLabel: string;
}) {
  if (isLoading) {
    return <p className="text-sm italic text-muted py-6">Loading…</p>;
  }
  if (rows.length === 0) {
    return <p className="text-sm italic text-muted py-6">{emptyLabel}</p>;
  }
  return (
    <div className="overflow-x-auto rounded-md border border-panel-border">
      <table className="w-full text-sm">
        <thead className="bg-panel-elev/60 text-muted uppercase text-xs tracking-wider">
          <tr>
            <th className="text-left font-semibold px-4 py-2">Device</th>
            <th className="text-left font-semibold px-4 py-2">Server</th>
            {showLevel && <th className="text-left font-semibold px-4 py-2 w-32">Level</th>}
          </tr>
        </thead>
        <tbody className="divide-y divide-panel-border">
          {rows.map((row, i) => (
            <tr key={`${row.device}:${row.server}:${i}`} className="hover:bg-panel-elev/40 transition-colors">
              <td className="px-4 py-2 text-text">{row.device}</td>
              <td className="px-4 py-2 text-text font-mono">{row.server}</td>
              {showLevel && <td className="px-4 py-2 text-text">{row.level || <Dash />}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AclMatrixTable({
  rows,
  devices,
  isLoading,
  expanded,
  onToggle,
}: {
  rows: ReturnType<typeof toAclMatrix>;
  devices: string[];
  isLoading: boolean;
  expanded: Set<string>;
  onToggle: (key: string) => void;
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
                      <Dash />
                    </td>
                  );
                }
                const isOpen = expanded.has(key);
                return (
                  <td key={device} className="px-4 py-2">
                    <button
                      type="button"
                      onClick={() => onToggle(key)}
                      className="text-left text-info hover:brightness-125 tabular-nums"
                      title={cell.type ?? undefined}
                    >
                      {isOpen ? '▾' : '▸'} {cell.ruleCount} rule{cell.ruleCount === 1 ? '' : 's'}
                    </button>
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

function Dash() {
  return <span className="text-muted italic">—</span>;
}
