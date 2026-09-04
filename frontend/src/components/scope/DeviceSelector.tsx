'use client';

import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getDevices, listSiteGroups, type DeviceGroup } from '@/services/api';
import type { Device } from '@/types/device';
import type { Scope } from './ScopeDashboard';
import { ChevronDownIcon, ChevronRightIcon } from '../Icon';

interface Props {
  scope: Scope;
  /** Names of the devices currently checked. Empty set = nothing selected. */
  value: Set<string>;
  onChange: (next: Set<string>) => void;
  /** Optional device name to force-select at device scope. */
  deviceName?: string;
}

/**
 * Scope-aware target picker used by every VLAN write modal (create/update/
 * remove). Everything is deselected by default so nothing bad happens on an
 * accidental submit.
 *
 * - Device scope: renders a static info line — the target is unambiguous.
 * - Group scope: flat checkbox list of every device in the group.
 * - Site scope: tree of groups → devices with tri-state parent checkboxes.
 * - Org scope: not implemented — the Org-level VLAN tab is disabled today,
 *   we'll add a Sites → Groups → Devices tier here when we open it.
 */
export function DeviceSelector({ scope, value, onChange, deviceName }: Props) {
  if (scope.kind === 'device') {
    return <DeviceOnly deviceName={deviceName ?? '(current device)'} />;
  }
  if (scope.kind === 'group') {
    return <GroupSelector groupId={scope.groupId} value={value} onChange={onChange} />;
  }
  if (scope.kind === 'site') {
    return <SiteSelector siteId={scope.siteId} value={value} onChange={onChange} />;
  }
  return (
    <p className="text-sm italic text-muted">
      Org-scope device picker will land alongside org-level VLAN actions.
    </p>
  );
}

/** Static info block for device scope — no interaction needed. */
function DeviceOnly({ deviceName }: { deviceName: string }) {
  return (
    <div className="rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm">
      Target device:{' '}
      <span className="font-semibold text-text">{deviceName}</span>
    </div>
  );
}

// ── Group scope ──────────────────────────────────────────────────────────────

function GroupSelector({
  groupId,
  value,
  onChange,
}: {
  groupId: number;
  value: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const { data: devices = [], isLoading, isError } = useQuery<Device[]>({
    queryKey: ['selector', 'devices', 'by-group', groupId],
    queryFn: async () => {
      const all = await getDevices();
      return all.filter((d) => d.device_group_id === groupId);
    },
  });

  if (isLoading) return <SelectorLoading />;
  if (isError) return <SelectorError text="Failed to load devices" />;
  if (devices.length === 0) return <SelectorEmpty text="No devices in this group." />;

  const names = devices.map((d) => d.name);
  return (
    <div className="flex flex-col gap-2">
      <SelectAllBar
        total={names.length}
        selected={countSelected(names, value)}
        onSelectAll={() => onChange(new Set(names))}
        onSelectNone={() => onChange(new Set())}
      />
      <div className="rounded-md border border-panel-border max-h-64 overflow-y-auto divide-y divide-panel-border">
        {devices.map((d) => (
          <DeviceRow
            key={d.name}
            name={d.name}
            checked={value.has(d.name)}
            onToggle={() => toggleName(value, d.name, onChange)}
          />
        ))}
      </div>
    </div>
  );
}

// ── Site scope ───────────────────────────────────────────────────────────────

function SiteSelector({
  siteId,
  value,
  onChange,
}: {
  siteId: number;
  value: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const groupsQuery = useQuery<DeviceGroup[]>({
    queryKey: ['selector', 'groups', siteId],
    queryFn: () => listSiteGroups(siteId),
  });
  const allDevicesQuery = useQuery<Device[]>({
    queryKey: ['selector', 'devices', 'by-site', siteId],
    queryFn: async () => {
      const all = await getDevices();
      return all.filter((d) => d.site_id === siteId);
    },
  });

  const isLoading = groupsQuery.isLoading || allDevicesQuery.isLoading;
  const isError = groupsQuery.isError || allDevicesQuery.isError;

  const devicesByGroup = useMemo(() => {
    const map = new Map<number, Device[]>();
    for (const d of allDevicesQuery.data ?? []) {
      const bucket = map.get(d.device_group_id) ?? [];
      bucket.push(d);
      map.set(d.device_group_id, bucket);
    }
    return map;
  }, [allDevicesQuery.data]);

  const allDeviceNames = useMemo(
    () => (allDevicesQuery.data ?? []).map((d) => d.name),
    [allDevicesQuery.data],
  );

  if (isLoading) return <SelectorLoading />;
  if (isError) return <SelectorError text="Failed to load site tree" />;
  if ((groupsQuery.data ?? []).length === 0) {
    return <SelectorEmpty text="No device groups in this site." />;
  }

  return (
    <div className="flex flex-col gap-2">
      <SelectAllBar
        total={allDeviceNames.length}
        selected={countSelected(allDeviceNames, value)}
        onSelectAll={() => onChange(new Set(allDeviceNames))}
        onSelectNone={() => onChange(new Set())}
      />
      <div className="rounded-md border border-panel-border max-h-72 overflow-y-auto">
        {(groupsQuery.data ?? []).map((group) => {
          const groupDevices = devicesByGroup.get(group.id) ?? [];
          return (
            <GroupNode
              key={group.id}
              group={group}
              devices={groupDevices}
              value={value}
              onChange={onChange}
            />
          );
        })}
      </div>
    </div>
  );
}

function GroupNode({
  group,
  devices,
  value,
  onChange,
}: {
  group: DeviceGroup;
  devices: Device[];
  value: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const groupDeviceNames = devices.map((d) => d.name);
  const selectedCount = countSelected(groupDeviceNames, value);
  const state = triState(groupDeviceNames.length, selectedCount);

  function toggleGroup() {
    if (state === 'checked') {
      // Remove every device of this group from the current selection.
      const next = new Set(value);
      for (const n of groupDeviceNames) next.delete(n);
      onChange(next);
    } else {
      const next = new Set(value);
      for (const n of groupDeviceNames) next.add(n);
      onChange(next);
    }
  }

  return (
    <div className="border-b border-panel-border last:border-b-0">
      <div className="flex items-center gap-2 px-3 py-2 hover:bg-panel-elev">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-label={open ? 'Collapse group' : 'Expand group'}
          disabled={groupDeviceNames.length === 0}
          className="text-muted hover:text-text disabled:opacity-40"
        >
          {open ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
        </button>
        <TriCheckbox state={state} onClick={toggleGroup} />
        <span className="text-sm font-medium text-text flex-1 truncate">
          {group.name}
        </span>
        <span className="text-xs text-muted tabular-nums">
          {selectedCount} / {groupDeviceNames.length}
        </span>
      </div>
      {open && groupDeviceNames.length > 0 && (
        <div className="pl-9 pr-3 pb-1 divide-y divide-panel-border/60">
          {devices.map((d) => (
            <DeviceRow
              key={d.name}
              name={d.name}
              checked={value.has(d.name)}
              onToggle={() => toggleName(value, d.name, onChange)}
              indent
            />
          ))}
        </div>
      )}
      {open && groupDeviceNames.length === 0 && (
        <p className="pl-9 py-2 text-xs italic text-muted">Empty group.</p>
      )}
    </div>
  );
}

// ── Shared bits ──────────────────────────────────────────────────────────────

function DeviceRow({
  name,
  checked,
  onToggle,
  indent,
}: {
  name: string;
  checked: boolean;
  onToggle: () => void;
  indent?: boolean;
}) {
  return (
    <label
      className={`flex items-center gap-2 py-1.5 text-sm text-text cursor-pointer hover:bg-panel-elev/60 ${
        indent ? 'px-1' : 'px-3'
      }`}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={onToggle}
        className="h-4 w-4 accent-info"
      />
      <span className="truncate">{name}</span>
    </label>
  );
}

function SelectAllBar({
  total,
  selected,
  onSelectAll,
  onSelectNone,
}: {
  total: number;
  selected: number;
  onSelectAll: () => void;
  onSelectNone: () => void;
}) {
  const allChecked = selected === total && total > 0;
  return (
    <div className="flex items-center justify-between text-xs text-muted">
      <span className="tabular-nums">
        <span className="text-text font-semibold">{selected}</span> / {total} selected
      </span>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onSelectAll}
          disabled={allChecked || total === 0}
          className="rounded px-2 py-0.5 border border-panel-border hover:bg-panel-elev disabled:opacity-40"
        >
          Select all
        </button>
        <button
          type="button"
          onClick={onSelectNone}
          disabled={selected === 0}
          className="rounded px-2 py-0.5 border border-panel-border hover:bg-panel-elev disabled:opacity-40"
        >
          Clear
        </button>
      </div>
    </div>
  );
}

function TriCheckbox({
  state,
  onClick,
}: {
  state: 'unchecked' | 'indeterminate' | 'checked';
  onClick: () => void;
}) {
  // Native indeterminate can only be set via ref. We use a plain <input> with
  // a ref-callback so the browser renders the tri-state affordance itself.
  return (
    <input
      type="checkbox"
      className="h-4 w-4 accent-info"
      checked={state === 'checked'}
      ref={(el) => {
        if (el) el.indeterminate = state === 'indeterminate';
      }}
      onChange={onClick}
      onClick={(e) => e.stopPropagation()}
    />
  );
}

function SelectorLoading() {
  return <p className="text-sm italic text-muted">Loading targets…</p>;
}

function SelectorError({ text }: { text: string }) {
  return <p className="text-sm text-danger">{text}</p>;
}

function SelectorEmpty({ text }: { text: string }) {
  return <p className="text-sm italic text-muted">{text}</p>;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function countSelected(names: string[], value: Set<string>): number {
  let count = 0;
  for (const n of names) if (value.has(n)) count += 1;
  return count;
}

function triState(
  total: number,
  selected: number,
): 'unchecked' | 'indeterminate' | 'checked' {
  if (selected === 0) return 'unchecked';
  if (selected === total) return 'checked';
  return 'indeterminate';
}

function toggleName(
  current: Set<string>,
  name: string,
  onChange: (next: Set<string>) => void,
): void {
  const next = new Set(current);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  onChange(next);
}
