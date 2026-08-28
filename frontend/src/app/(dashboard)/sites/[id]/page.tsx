'use client';

import { useMemo, useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import {
  getSite,
  listSiteGroups,
  getDevices,
  moveDevice,
} from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import type { Site } from '@/types/site';
import type { Device } from '@/types/device';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

export default function SiteDetailPage() {
  // Next.js 16 App Router — `params` from `useParams` is synchronous in a
  // client component (server components need `await params`).
  const params = useParams<{ id: string }>();
  const siteId = Number(params.id);

  const [movingDevice, setMovingDevice] = useState<Device | null>(null);
  const [moveTargetGroupId, setMoveTargetGroupId] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const msgTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (msgTimerRef.current !== null) {
      clearTimeout(msgTimerRef.current);
      msgTimerRef.current = null;
    }
    if (!successMessage && !errorMessage) return;
    msgTimerRef.current = setTimeout(() => {
      setSuccessMessage(null);
      setErrorMessage(null);
      msgTimerRef.current = null;
    }, 4000);
    return () => {
      if (msgTimerRef.current !== null) clearTimeout(msgTimerRef.current);
    };
  }, [successMessage, errorMessage]);

  const {
    data: site,
    isLoading: siteLoading,
    error: siteError,
    refetch: refetchSite,
  } = useQuery<Site>({
    queryKey: ['site', siteId],
    queryFn: () => getSite(siteId),
    enabled: Number.isFinite(siteId),
  });

  const {
    data: groups,
    isLoading: groupsLoading,
    error: groupsError,
    refetch: refetchGroups,
  } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', siteId],
    queryFn: () => listSiteGroups(siteId),
    enabled: Number.isFinite(siteId),
  });

  const { data: allDevices, refetch: refetchDevices } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  // Group devices by device_group_id for cheap render — this is the
  // authoritative FK post-Phase 4 (M3 flipped it NOT NULL).
  const devicesByGroup = useMemo(() => {
    const map = new Map<number, Device[]>();
    for (const d of allDevices ?? []) {
      if (d.site_id !== siteId) continue;
      const arr = map.get(d.device_group_id) ?? [];
      arr.push(d);
      map.set(d.device_group_id, arr);
    }
    return map;
  }, [allDevices, siteId]);

  const groupList = groups ?? [];

  async function handleMove() {
    if (!movingDevice) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      const targetId = moveTargetGroupId === '' ? null : Number(moveTargetGroupId);
      await moveDevice(movingDevice.name, targetId);
      const name = movingDevice.name;
      setMovingDevice(null);
      setMoveTargetGroupId('');
      await Promise.all([refetchDevices(), refetchGroups()]);
      setSuccessMessage(`Device ${name} moved successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Move failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  if (siteLoading || groupsLoading) {
    return (
      <div className="py-12">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  if (siteError || !site) {
    return (
      <div>
        <PageHeader title="Site not found" />
        <ErrorMessage error={extractMessage(siteError, 'Could not load site')} />
        <div className="mt-4">
          <Link href="/sites" className="text-blue-600 hover:underline text-sm">
            ← Back to sites
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title={site.name}
        actions={
          <Link
            href="/sites"
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            ← All sites
          </Link>
        }
      />

      <div className="flex items-center gap-2 mb-4 text-sm text-gray-600">
        <span>{site.description ?? 'No description'}</span>
        {site.kind === 'BASE_INFRASTRUCTURE' && (
          <span
            title="System-managed base site (D14)"
            className="text-[10px] px-2 py-0.5 bg-amber-100 text-amber-800 rounded-full uppercase tracking-wide"
          >
            Base Infra
          </span>
        )}
      </div>

      {successMessage && (
        <div className="mb-4 text-sm text-green-700">✓ {successMessage}</div>
      )}
      {errorMessage && (
        <div className="mb-4">
          <ErrorMessage error={`✗ ${errorMessage}`} />
        </div>
      )}

      {groupsError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(groupsError, 'Could not load groups')} />
        </div>
      ) : groupList.length === 0 ? (
        <p className="py-6 text-gray-500 text-sm">No groups in this site.</p>
      ) : (
        <div className="space-y-6">
          {groupList.map((group) => {
            const devices = devicesByGroup.get(group.id) ?? [];
            const isDefaultGroup = group.id === site.default_group_id;
            return (
              <section
                key={group.id}
                className="border border-gray-200 rounded-lg overflow-hidden"
              >
                <header className="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center gap-2">
                  <h2 className="text-sm font-semibold text-gray-800">
                    {group.name}
                  </h2>
                  {isDefaultGroup && (
                    <span className="text-[10px] px-2 py-0.5 bg-indigo-100 text-indigo-700 rounded-full uppercase tracking-wide">
                      Default
                    </span>
                  )}
                  <span className="text-xs text-gray-500 ml-auto">
                    {devices.length} device{devices.length === 1 ? '' : 's'}
                  </span>
                </header>
                {devices.length === 0 ? (
                  <p className="px-4 py-6 text-center text-sm text-gray-400">
                    No devices in this group.
                  </p>
                ) : (
                  <table className="w-full border-collapse text-sm">
                    <thead>
                      <tr className="border-b border-gray-100 bg-white">
                        <th className="text-left px-4 py-2 font-medium text-gray-700">Device</th>
                        <th className="text-left px-4 py-2 font-medium text-gray-700">Host</th>
                        <th className="text-left px-4 py-2 font-medium text-gray-700">Vendor</th>
                        <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {devices.map((d) => (
                        <tr key={d.id} className="border-b border-gray-100 hover:bg-gray-50">
                          <td className="px-4 py-2 font-mono text-xs">{d.name}</td>
                          <td className="px-4 py-2">{d.host}</td>
                          <td className="px-4 py-2">{d.vendor}</td>
                          <td className="px-4 py-2">
                            <button
                              onClick={() => {
                                setMovingDevice(d);
                                setMoveTargetGroupId(String(d.device_group_id));
                              }}
                              className="px-2 py-1 text-xs text-indigo-600 border border-indigo-300 rounded hover:bg-indigo-50"
                            >
                              Move…
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
            );
          })}
        </div>
      )}

      {/* ── Move dialog ────────────────────────────────────────────────── */}
      {movingDevice && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-md bg-white rounded-lg shadow-xl p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-1">
              Move device
            </h2>
            <p className="text-sm text-gray-600 mb-4">
              <span className="font-mono">{movingDevice.name}</span> is currently
              in <span className="font-medium">{movingDevice.device_group_name}</span>{' '}
              (site <span className="font-medium">{movingDevice.site_name}</span>).
              Pick a target group within this site, or use “Reset to Default” to
              send it back to the Default group.
            </p>
            <label className="block text-xs font-medium text-gray-700 mb-1">
              Target group
            </label>
            <select
              value={moveTargetGroupId}
              onChange={(e) => setMoveTargetGroupId(e.target.value)}
              disabled={isSubmitting}
              className="w-full border border-gray-300 rounded-md px-3 py-1.5 text-sm mb-4 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            >
              {groupList.map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}{g.id === movingDevice.device_group_id ? ' (current)' : ''}
                </option>
              ))}
            </select>
            <div className="flex justify-between items-center gap-3">
              <button
                onClick={() => setMoveTargetGroupId('')}
                disabled={isSubmitting}
                title="Send device to this site's Default group (D8)"
                className="px-3 py-1.5 text-xs text-indigo-700 border border-indigo-300 rounded hover:bg-indigo-50 disabled:opacity-50"
              >
                Reset to Default
              </button>
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    setMovingDevice(null);
                    setMoveTargetGroupId('');
                  }}
                  disabled={isSubmitting}
                  className="px-3 py-1.5 text-sm text-gray-700 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleMove}
                  disabled={isSubmitting}
                  className="px-3 py-1.5 text-sm text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {isSubmitting ? 'Moving…' : 'Move device'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="mt-6 text-xs text-gray-400">
        Site created {new Date(site.created_at).toLocaleString()} · updated{' '}
        {new Date(site.updated_at).toLocaleString()}
      </div>
    </div>
  );
}
