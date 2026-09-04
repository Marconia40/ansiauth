'use client';

import { useState, useEffect, useRef } from 'react';
import { useAuth } from '@/context/AuthContext';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import {
  getDevices,
  createDevice,
  updateDevice,
  deleteDevice,
  getSites,
  listSiteGroups,
  moveDevice,
  extractMessage,
} from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import type { Device, DeviceUpdate, Vendor } from '@/types/device';
import type { Site } from '@/types/site';

const VENDOR_LABELS: Record<Vendor, string> = {
  cisco: 'Cisco IOS',
  huawei: 'Huawei',
};

export default function DevicesPage() {
  const { user } = useAuth();
  const canMutate = !!user && user.role !== 'observer';

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingDeviceName, setDeletingDeviceName] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // ── Create form ──────────────────────────────────────────────────────────
  const [newName, setNewName] = useState('');
  const [newHost, setNewHost] = useState('');
  const [newVendor, setNewVendor] = useState<Vendor>('cisco');
  const [newPlatform, setNewPlatform] = useState('');
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  // MSP: Phase 4 — Site is required. Empty string means "not selected"; the
  // submit button is disabled until a site is picked.
  const [newSiteId, setNewSiteId] = useState<string>('');
  const [newGroupId, setNewGroupId] = useState<string>('');

  // ── Edit form (site/group NOT edited here per D20 — use Move… instead) ──
  const [editingDeviceName, setEditingDeviceName] = useState<string | null>(null);
  const [editingHost, setEditingHost] = useState('');
  const [editingVendor, setEditingVendor] = useState<Vendor>('cisco');
  const [editingPlatform, setEditingPlatform] = useState('');
  const [editingUsername, setEditingUsername] = useState('');
  const [editingPassword, setEditingPassword] = useState('');

  // ── Move dialog state ────────────────────────────────────────────────────
  const [movingDevice, setMovingDevice] = useState<Device | null>(null);
  const [moveTargetGroupId, setMoveTargetGroupId] = useState<string>(''); // '' → send null (→ site default)

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
    data: devices,
    isLoading,
    isFetching,
    error: devicesError,
    refetch,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const { data: sites } = useQuery<Site[]>({
    queryKey: ['sites'],
    queryFn: getSites,
  });
  const siteList = sites ?? [];

  // Cascading group list — refetches whenever the selected Site changes.
  const { data: newSiteGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', newSiteId],
    queryFn: () => listSiteGroups(Number(newSiteId)),
    enabled: !!newSiteId,
  });

  // Preselect the Default group whenever the site changes and the groups
  // finish loading. The user can override before submit.
  useEffect(() => {
    if (!newSiteGroups) return;
    const site = siteList.find((s) => String(s.id) === newSiteId);
    if (!site) return;
    setNewGroupId(String(site.default_group_id));
  }, [newSiteGroups, newSiteId, siteList]);

  // Move-dialog group list — same query for the current move target.
  const moveSiteId = movingDevice?.site_id;
  const { data: moveTargetGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', moveSiteId],
    queryFn: () => listSiteGroups(moveSiteId!),
    enabled: moveSiteId != null,
  });

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) { setErrorMessage('Device name is required'); return; }
    if (!newHost.trim()) { setErrorMessage('Host is required'); return; }
    if (!newPlatform.trim()) { setErrorMessage('Platform is required'); return; }
    if (!newUsername.trim()) { setErrorMessage('Username is required'); return; }
    if (!newPassword.trim()) { setErrorMessage('Password is required'); return; }
    if (!newSiteId) { setErrorMessage('Site is required'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    const deviceName = newName.trim();
    try {
      await createDevice({
        name: deviceName,
        host: newHost.trim(),
        vendor: newVendor,
        platform: newPlatform.trim(),
        username: newUsername.trim(),
        password: newPassword.trim(),
        site_id: Number(newSiteId),
        device_group_id: newGroupId ? Number(newGroupId) : undefined,
      });
      setNewName('');
      setNewHost('');
      setNewVendor('cisco');
      setNewPlatform('');
      setNewUsername('');
      setNewPassword('');
      setNewSiteId('');
      setNewGroupId('');
      await refetch();
      setSuccessMessage(`Device ${deviceName} created successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  function handleEditStart(device: Device) {
    setEditingDeviceName(device.name);
    setEditingHost(device.host);
    setEditingVendor(device.vendor);
    setEditingPlatform(device.platform);
    setEditingUsername(device.username);
    setEditingPassword('');
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingDeviceName(null);
    setEditingHost('');
    setEditingVendor('cisco');
    setEditingPlatform('');
    setEditingUsername('');
    setEditingPassword('');
  }

  async function handleUpdate() {
    if (!editingHost.trim()) { setErrorMessage('Host is required'); return; }
    if (!editingPlatform.trim()) { setErrorMessage('Platform is required'); return; }
    if (!editingUsername.trim()) { setErrorMessage('Username is required'); return; }
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    const deviceName = editingDeviceName!;
    try {
      // MSP: Phase 4 — site_id/device_group_id are no longer accepted here.
      // Use the "Move…" row action instead.
      const body: DeviceUpdate = {
        host: editingHost.trim(),
        vendor: editingVendor,
        platform: editingPlatform.trim(),
        username: editingUsername.trim(),
      };
      if (editingPassword.trim()) {
        body.password = editingPassword.trim();
      }
      await updateDevice(deviceName, body);
      handleEditCancel();
      await refetch();
      setSuccessMessage(`Device ${deviceName} updated successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Update failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleDelete(device: Device) {
    if (!window.confirm(`Delete device ${device.name}?`)) return;
    setDeletingDeviceName(device.name);
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await deleteDevice(device.name);
      await refetch();
      setSuccessMessage(`Device ${device.name} deleted successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setIsSubmitting(false);
      setDeletingDeviceName(null);
    }
  }

  function handleMoveStart(device: Device) {
    setMovingDevice(device);
    setMoveTargetGroupId(String(device.device_group_id));
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleMoveCancel() {
    setMovingDevice(null);
    setMoveTargetGroupId('');
  }

  async function handleMoveConfirm() {
    if (!movingDevice) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      // Empty string → send null → move to current site's Default group (D8).
      const targetId = moveTargetGroupId === '' ? null : Number(moveTargetGroupId);
      await moveDevice(movingDevice.name, targetId);
      const deviceName = movingDevice.name;
      handleMoveCancel();
      await refetch();
      setSuccessMessage(`Device ${deviceName} moved successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Move failed'));
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div>
      <PageHeader
        title="Device Management"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching || isSubmitting}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">Manage network devices in the inventory</p>

      {canMutate && (
      <form onSubmit={handleCreate} className="flex flex-wrap gap-2 mb-6 items-center">
        <input
          type="text"
          placeholder="Name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <input
          type="text"
          placeholder="Host / IP"
          value={newHost}
          onChange={(e) => setNewHost(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <select
          value={newVendor}
          onChange={(e) => setNewVendor(e.target.value as Vendor)}
          disabled={isSubmitting}
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        >
          <option value="cisco">Cisco IOS</option>
          <option value="huawei">Huawei</option>
        </select>
        <input
          type="text"
          placeholder="Platform"
          value={newPlatform}
          onChange={(e) => setNewPlatform(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <input
          type="text"
          placeholder="Username"
          value={newUsername}
          onChange={(e) => setNewUsername(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <input
          type="password"
          placeholder="Password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        />
        <select
          value={newSiteId}
          onChange={(e) => {
            setNewSiteId(e.target.value);
            setNewGroupId(''); // will be repopulated when groups load
          }}
          disabled={isSubmitting}
          aria-label="Site"
          required
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        >
          <option value="">Select site…</option>
          {siteList.map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <select
          value={newGroupId}
          onChange={(e) => setNewGroupId(e.target.value)}
          disabled={isSubmitting || !newSiteId}
          aria-label="Group"
          className="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
        >
          {!newSiteId ? (
            <option value="">Select site first…</option>
          ) : (
            (newSiteGroups ?? []).map((g) => (
              <option key={g.id} value={g.id}>{g.name}</option>
            ))
          )}
        </select>
        <button
          type="submit"
          disabled={
            isSubmitting ||
            !newName.trim() || !newHost.trim() || !newPlatform.trim() ||
            !newUsername.trim() || !newPassword.trim() || !newSiteId
          }
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isSubmitting && deletingDeviceName === null && editingDeviceName === null && !movingDevice ? 'Creating...' : 'Add Device'}
        </button>
      </form>
      )}

      {successMessage && (
        <div className="mb-4 text-sm text-green-700">&#10003; {successMessage}</div>
      )}
      {errorMessage && (
        <div className="mb-4">
          <ErrorMessage error={`✗ ${errorMessage}`} />
        </div>
      )}

      {isLoading ? (
        <div className="py-12">
          <LoadingSpinner size="lg" />
        </div>
      ) : devicesError ? (
        <div className="py-6">
          <ErrorMessage error={extractMessage(devicesError, 'Could not load devices')} />
          <button
            onClick={() => refetch()}
            className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Retry
          </button>
        </div>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700">Name</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Host</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Vendor</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Platform</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Username</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Site</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Group</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
            </tr>
          </thead>
          <tbody>
            {devices && devices.length > 0 ? (
              devices.map((device) => {
                const isEditing = editingDeviceName === device.name;
                return (
                  <tr key={device.id} className="border-b border-gray-100 hover:bg-gray-50">
                    <td className="px-4 py-2 text-gray-900 font-mono text-xs">{device.name}</td>
                    <td className="px-4 py-2 text-gray-900">
                      {isEditing ? (
                        <input
                          type="text"
                          value={editingHost}
                          onChange={(e) => setEditingHost(e.target.value)}
                          disabled={isSubmitting}
                          autoFocus
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                        />
                      ) : (
                        device.host
                      )}
                    </td>
                    <td className="px-4 py-2 text-gray-900">
                      {isEditing ? (
                        <select
                          value={editingVendor}
                          onChange={(e) => setEditingVendor(e.target.value as Vendor)}
                          disabled={isSubmitting}
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                        >
                          <option value="cisco">Cisco IOS</option>
                          <option value="huawei">Huawei</option>
                        </select>
                      ) : (
                        VENDOR_LABELS[device.vendor] ?? device.vendor
                      )}
                    </td>
                    <td className="px-4 py-2 text-gray-900">
                      {isEditing ? (
                        <input
                          type="text"
                          value={editingPlatform}
                          onChange={(e) => setEditingPlatform(e.target.value)}
                          disabled={isSubmitting}
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                        />
                      ) : (
                        device.platform
                      )}
                    </td>
                    <td className="px-4 py-2 text-gray-900">
                      {isEditing ? (
                        <input
                          type="text"
                          value={editingUsername}
                          onChange={(e) => setEditingUsername(e.target.value)}
                          disabled={isSubmitting}
                          className="border border-gray-300 rounded-md px-2 py-1 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                        />
                      ) : (
                        device.username
                      )}
                    </td>
                    <td className="px-4 py-2 text-gray-900">{device.site_name}</td>
                    <td className="px-4 py-2 text-gray-900">{device.device_group_name}</td>
                    <td className="px-4 py-2">
                      {canMutate && (isEditing ? (
                        <div className="flex flex-wrap gap-2 items-center">
                          <input
                            type="password"
                            placeholder="New password"
                            value={editingPassword}
                            onChange={(e) => setEditingPassword(e.target.value)}
                            disabled={isSubmitting}
                            className="border border-gray-300 rounded-md px-2 py-1 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                          />
                          <button
                            onClick={handleUpdate}
                            disabled={
                              isSubmitting ||
                              !editingHost.trim() || !editingPlatform.trim() || !editingUsername.trim()
                            }
                            className="px-2 py-1 text-xs text-white bg-blue-600 border border-blue-600 rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {isSubmitting && deletingDeviceName === null ? 'Saving...' : 'Save'}
                          </button>
                          <button
                            onClick={handleEditCancel}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-gray-600 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleEditStart(device)}
                            disabled={isSubmitting || editingDeviceName !== null}
                            className="px-2 py-1 text-xs text-blue-600 border border-blue-300 rounded hover:bg-blue-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Edit
                          </button>
                          <button
                            onClick={() => handleMoveStart(device)}
                            disabled={isSubmitting || editingDeviceName !== null}
                            className="px-2 py-1 text-xs text-indigo-600 border border-indigo-300 rounded hover:bg-indigo-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Move…
                          </button>
                          <button
                            onClick={() => handleDelete(device)}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {deletingDeviceName === device.name ? 'Deleting...' : 'Delete'}
                          </button>
                        </div>
                      ))}
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={8} className="px-4 py-8 text-center text-gray-400">
                  No devices registered yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
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
              in <span className="font-medium">{movingDevice.site_name}</span> /{' '}
              <span className="font-medium">{movingDevice.device_group_name}</span>.
              Pick a target group within the same site, or use “Reset to
              Default” to send it back to this site's Default group.
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
              {(moveTargetGroups ?? []).map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}{g.id === movingDevice.device_group_id ? ' (current)' : ''}
                </option>
              ))}
            </select>
            <div className="flex justify-between items-center gap-3">
              <button
                onClick={() => {
                  setMoveTargetGroupId('');
                }}
                disabled={isSubmitting}
                title="Send device to this site's Default group (D8)"
                className="px-3 py-1.5 text-xs text-indigo-700 border border-indigo-300 rounded hover:bg-indigo-50 disabled:opacity-50"
              >
                Reset to Default
              </button>
              <div className="flex gap-2">
                <button
                  onClick={handleMoveCancel}
                  disabled={isSubmitting}
                  className="px-3 py-1.5 text-sm text-gray-700 border border-gray-300 rounded hover:bg-gray-50 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleMoveConfirm}
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
    </div>
  );
}
