'use client';

import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { usePathname } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import {
  getDevices,
  getSites,
  listSiteGroups,
  type DeviceGroup,
} from '@/services/api';
import type { Site } from '@/types/site';
import type { Device } from '@/types/device';
import { ChevronDownIcon, ChevronRightIcon } from './Icon';

const EXPAND_STORAGE_KEY = 'ansiauth.sidebar.expanded';

/** Runtime expansion state keyed by opaque node id. */
type ExpandedMap = Record<string, boolean>;

function loadExpanded(): ExpandedMap {
  if (typeof window === 'undefined') return { 'org': true };
  try {
    const raw = window.sessionStorage.getItem(EXPAND_STORAGE_KEY);
    if (!raw) return { 'org': true };
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? (parsed as ExpandedMap) : { 'org': true };
  } catch {
    return { 'org': true };
  }
}

function saveExpanded(map: ExpandedMap): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(EXPAND_STORAGE_KEY, JSON.stringify(map));
  } catch {
    /* ignore */
  }
}

export function Sidebar() {
  const [expanded, setExpanded] = useState<ExpandedMap>({ 'org': true });

  useEffect(() => {
    // Hydrate from sessionStorage after mount to avoid SSR/CSR mismatch.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setExpanded(loadExpanded());
  }, []);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      saveExpanded(next);
      return next;
    });
  }, []);

  const isOpen = (id: string) => Boolean(expanded[id]);

  return (
    <aside className="w-64 shrink-0 bg-sidebar text-text border-r border-panel-border flex flex-col">
      <div className="p-3 overflow-y-auto text-sm">
        <TreeRow
          id="org"
          label="Organization"
          href="/"
          level={0}
          hasChildren
          open={isOpen('org')}
          onToggle={() => toggle('org')}
        />
        {isOpen('org') && <SitesLevel isOpen={isOpen} toggle={toggle} />}
      </div>
    </aside>
  );
}

function SitesLevel({
  isOpen,
  toggle,
}: {
  isOpen: (id: string) => boolean;
  toggle: (id: string) => void;
}) {
  const { data, isLoading, isError } = useQuery<Site[]>({
    queryKey: ['sidebar', 'sites'],
    queryFn: getSites,
  });

  if (isLoading) return <Placeholder level={1} text="Loading sites…" />;
  if (isError) return <Placeholder level={1} text="Failed to load sites" tone="danger" />;
  if (!data || data.length === 0) return <Placeholder level={1} text="No sites available" />;

  return (
    <>
      {data.map((site) => {
        const id = `site:${site.id}`;
        return (
          <div key={site.id}>
            <TreeRow
              id={id}
              label={site.name}
              href={`/sites/${site.id}`}
              level={1}
              hasChildren
              open={isOpen(id)}
              onToggle={() => toggle(id)}
            />
            {isOpen(id) && (
              <GroupsLevel siteId={site.id} isOpen={isOpen} toggle={toggle} />
            )}
          </div>
        );
      })}
    </>
  );
}

function GroupsLevel({
  siteId,
  isOpen,
  toggle,
}: {
  siteId: number;
  isOpen: (id: string) => boolean;
  toggle: (id: string) => void;
}) {
  const { data, isLoading, isError } = useQuery<DeviceGroup[]>({
    queryKey: ['sidebar', 'groups', siteId],
    queryFn: () => listSiteGroups(siteId),
  });

  if (isLoading) return <Placeholder level={2} text="Loading groups…" />;
  if (isError) return <Placeholder level={2} text="Failed to load groups" tone="danger" />;
  if (!data || data.length === 0) return <Placeholder level={2} text="No groups" />;

  return (
    <>
      {data.map((group) => {
        const id = `group:${group.id}`;
        return (
          <div key={group.id}>
            <TreeRow
              id={id}
              label={group.name}
              href={`/groups/${group.id}`}
              level={2}
              hasChildren
              open={isOpen(id)}
              onToggle={() => toggle(id)}
            />
            {isOpen(id) && <DevicesLevel groupId={group.id} />}
          </div>
        );
      })}
    </>
  );
}

function DevicesLevel({ groupId }: { groupId: number }) {
  // Reuses the same query slice as the Inventory page so navigating both
  // views doesn't re-fetch.
  const { data, isLoading, isError } = useQuery<Device[]>({
    queryKey: ['sidebar', 'devices', 'by-group', groupId],
    queryFn: async () => {
      const all = await getDevices();
      return all.filter((d) => d.device_group_id === groupId);
    },
  });

  if (isLoading) return <Placeholder level={3} text="Loading devices…" />;
  if (isError) return <Placeholder level={3} text="Failed to load devices" tone="danger" />;
  if (!data || data.length === 0) return <Placeholder level={3} text="No devices" />;

  return (
    <>
      {data.map((device) => (
        <TreeRow
          key={device.name}
          id={`device:${device.name}`}
          label={device.name}
          href={`/devices/${encodeURIComponent(device.name)}`}
          level={3}
          hasChildren={false}
        />
      ))}
    </>
  );
}

function TreeRow({
  label,
  href,
  level,
  hasChildren,
  open,
  onToggle,
}: {
  id: string;
  label: string;
  href: string;
  level: number;
  hasChildren: boolean;
  open?: boolean;
  onToggle?: () => void;
}) {
  const pathname = usePathname();
  const active = pathname === href;

  const padding = `${0.5 + level * 0.75}rem`;

  return (
    <div
      className={`group flex items-center rounded-md ${
        active ? 'bg-sidebar-active text-white' : 'hover:bg-sidebar-hover'
      }`}
    >
      {hasChildren ? (
        <button
          type="button"
          onClick={onToggle}
          aria-label={open ? `Collapse ${label}` : `Expand ${label}`}
          className="h-7 w-6 flex items-center justify-center text-muted hover:text-text shrink-0"
          style={{ marginLeft: padding }}
        >
          {open ? <ChevronDownIcon size={14} /> : <ChevronRightIcon size={14} />}
        </button>
      ) : (
        <span
          className="h-7 w-6 shrink-0"
          style={{ marginLeft: padding }}
        />
      )}
      <Link
        href={href}
        className={`flex-1 py-1 pr-3 truncate text-sm ${
          active ? 'font-semibold' : 'text-text/90'
        }`}
      >
        {label}
      </Link>
    </div>
  );
}

function Placeholder({
  level,
  text,
  tone,
}: {
  level: number;
  text: string;
  tone?: 'danger';
}) {
  return (
    <div
      className={`px-2 py-1 text-xs italic ${
        tone === 'danger' ? 'text-danger' : 'text-muted'
      }`}
      style={{ paddingLeft: `${0.75 + level * 0.75 + 1.5}rem` }}
    >
      {text}
    </div>
  );
}
