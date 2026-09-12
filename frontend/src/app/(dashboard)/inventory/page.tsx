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
} from '@/services/api';
import { useScope } from '@/context/ScopeContext';
import type { DeviceGroup } from '@/services/api';
import type { AuthMethod, Device, DeviceUpdate, Vendor } from '@/types/device';
import type { Site } from '@/types/site';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

const VENDOR_LABELS: Record<Vendor, string> = {
  cisco_ios: 'Cisco IOS',
  huawei_vrp: 'Huawei VRP',
};

// Free-text platform was error-prone (users don't know the exact syntax the
// Ansible collections expect). Constrain to the values the backend vendor
// drivers actually consume — extend a vendor's array when a new family is
// supported.
const PLATFORMS_BY_VENDOR: Record<Vendor, { value: string; label: string }[]> = {
  cisco_ios: [{ value: 'ios', label: 'IOS' }],
  huawei_vrp: [{ value: 'ce', label: 'CE (VRP)' }],
};

function defaultPlatformFor(vendor: Vendor): string {
  return PLATFORMS_BY_VENDOR[vendor][0]?.value ?? '';
}

export default function InventoryPage() {
  const { user } = useAuth();
  const canMutate = !!user && user.role !== 'observer';

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [deletingDeviceName, setDeletingDeviceName] = useState<string | null>(null);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // ── Create form ──────────────────────────────────────────────────────────
  const [newName, setNewName] = useState('');
  const [newHost, setNewHost] = useState('');
  const [newVendor, setNewVendor] = useState<Vendor>('cisco_ios');
  const [newPlatform, setNewPlatform] = useState<string>(defaultPlatformFor('cisco_ios'));
  const [newUsername, setNewUsername] = useState('');
  const [newAuthMethod, setNewAuthMethod] = useState<AuthMethod>('password');
  const [newPassword, setNewPassword] = useState('');
  const [newPrivateKey, setNewPrivateKey] = useState('');
  // MSP: Phase 4 — Site is required. Empty string means "not selected"; the
  // submit button is disabled until a site is picked.
  const [newSiteId, setNewSiteId] = useState<string>('');
  const [newGroupId, setNewGroupId] = useState<string>('');

  // ── Edit form (site/group NOT edited here per D20 — use Move… instead) ──
  const [editingDeviceName, setEditingDeviceName] = useState<string | null>(null);
  const [editingHost, setEditingHost] = useState('');
  const [editingVendor, setEditingVendor] = useState<Vendor>('cisco_ios');
  const [editingPlatform, setEditingPlatform] = useState<string>(defaultPlatformFor('cisco_ios'));
  const [editingUsername, setEditingUsername] = useState('');
  // Auth method the edit row is set to try to switch to. Defaults to the
  // device's current method — the field for whichever secret ISN'T shown
  // stays empty and unsent, matching "leave password blank to keep it".
  const [editingAuthMethod, setEditingAuthMethod] = useState<AuthMethod>('password');
  const [editingPassword, setEditingPassword] = useState('');
  const [editingPrivateKey, setEditingPrivateKey] = useState('');

  // ── Move dialog state ────────────────────────────────────────────────────
  const [movingDevice, setMovingDevice] = useState<Device | null>(null);
  const [moveTargetSiteId, setMoveTargetSiteId] = useState<string>('');
  const [moveTargetGroupId, setMoveTargetGroupId] = useState<string>('');

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

  // Client-side lens over the caller's visibility — when the topbar
  // scope is a specific site, narrow the device list to that site.
  // Backend still returns only devices the caller can see; this filter
  // is UX-only. See docs/USER_PERMISSIONS_UX_REDESIGN.md §3.1/§3.7.
  const { selectedScope } = useScope();
  const scopedSiteId =
    selectedScope.kind === 'site' ? selectedScope.siteId : null;
  const visibleDevices =
    scopedSiteId != null && devices
      ? devices.filter((d) => d.site_id === scopedSiteId)
      : devices;
  const scopedSiteName =
    scopedSiteId != null
      ? siteList.find((s) => s.id === scopedSiteId)?.name
      : null;

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

  // Move-dialog group list — driven by the *target* site the operator
  // picks, not the device's current site. Cross-site moves work as long
  // as the caller has admin on both sides (backend gates via
  // move_device_cross_site).
  const moveTargetSiteIdNum = moveTargetSiteId ? Number(moveTargetSiteId) : null;
  const { data: moveTargetGroups } = useQuery<DeviceGroup[]>({
    queryKey: ['site-groups', moveTargetSiteIdNum],
    queryFn: () => listSiteGroups(moveTargetSiteIdNum!),
    enabled: moveTargetSiteIdNum != null,
  });

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) { setErrorMessage('Device name is required'); return; }
    if (!newHost.trim()) { setErrorMessage('Host is required'); return; }
    if (!newPlatform.trim()) { setErrorMessage('Platform is required'); return; }
    if (!newUsername.trim()) { setErrorMessage('Username is required'); return; }
    if (newAuthMethod === 'password' && !newPassword.trim()) { setErrorMessage('Password is required'); return; }
    if (newAuthMethod === 'key' && !newPrivateKey.trim()) { setErrorMessage('Private key is required'); return; }
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
        auth_method: newAuthMethod,
        ...(newAuthMethod === 'password'
          ? { password: newPassword.trim() }
          : { private_key: newPrivateKey.trim() }),
        site_id: Number(newSiteId),
        device_group_id: newGroupId ? Number(newGroupId) : undefined,
      });
      setNewName('');
      setNewHost('');
      setNewVendor('cisco_ios');
      setNewPlatform(defaultPlatformFor('cisco_ios'));
      setNewUsername('');
      setNewAuthMethod('password');
      setNewPassword('');
      setNewPrivateKey('');
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
    setEditingAuthMethod(device.auth_method);
    setEditingPassword('');
    setEditingPrivateKey('');
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleEditCancel() {
    setEditingDeviceName(null);
    setEditingHost('');
    setEditingVendor('cisco_ios');
    setEditingPlatform(defaultPlatformFor('cisco_ios'));
    setEditingUsername('');
    setEditingAuthMethod('password');
    setEditingPassword('');
    setEditingPrivateKey('');
  }

  async function handleUpdate() {
    if (!editingHost.trim()) { setErrorMessage('Host is required'); return; }
    if (!editingPlatform.trim()) { setErrorMessage('Platform is required'); return; }
    if (!editingUsername.trim()) { setErrorMessage('Username is required'); return; }
    const currentDevice = devices?.find((d) => d.name === editingDeviceName);
    const switchingAuthMethod = !!currentDevice && editingAuthMethod !== currentDevice.auth_method;
    if (switchingAuthMethod && editingAuthMethod === 'password' && !editingPassword.trim()) {
      setErrorMessage('Password is required to switch to password auth');
      return;
    }
    if (switchingAuthMethod && editingAuthMethod === 'key' && !editingPrivateKey.trim()) {
      setErrorMessage('Private key is required to switch to key auth');
      return;
    }
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
      // Only include auth_method + the matching secret when a new secret was
      // actually typed — leaving both blank keeps the stored credential as
      // it is, same pattern the plain password field already had.
      if (editingAuthMethod === 'password' && editingPassword.trim()) {
        body.auth_method = 'password';
        body.password = editingPassword.trim();
      } else if (editingAuthMethod === 'key' && editingPrivateKey.trim()) {
        body.auth_method = 'key';
        body.private_key = editingPrivateKey.trim();
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
    setMoveTargetSiteId(String(device.site_id));
    setMoveTargetGroupId(String(device.device_group_id));
    setSuccessMessage(null);
    setErrorMessage(null);
  }

  function handleMoveCancel() {
    setMovingDevice(null);
    setMoveTargetSiteId('');
    setMoveTargetGroupId('');
  }

  // Cross-site jumps: when the operator picks a new target site, jump
  // the target group to that site's Default so the user isn't stuck with
  // a stale groupId that belongs to the previous site.
  function handleMoveTargetSiteChange(nextSiteIdStr: string) {
    setMoveTargetSiteId(nextSiteIdStr);
    const site = siteList.find((s) => String(s.id) === nextSiteIdStr);
    setMoveTargetGroupId(site ? String(site.default_group_id) : '');
  }

  async function handleMoveConfirm() {
    if (!movingDevice) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      // Empty string → send null → move to source site's Default group (D8
      // shortcut). Any concrete groupId lands the device there directly.
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
        title="Inventory"
        actions={
          <button
            onClick={() => refetch()}
            disabled={isLoading || isFetching || isSubmitting}
            className="px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-muted mb-6">Manage network devices in the inventory</p>

      {canMutate && (
      <form onSubmit={handleCreate} className="flex flex-wrap gap-2 mb-6 items-center">
        <input
          type="text"
          placeholder="Name"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        />
        <input
          type="text"
          placeholder="Host / IP"
          value={newHost}
          onChange={(e) => setNewHost(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        />
        <select
          value={newVendor}
          onChange={(e) => {
            const nextVendor = e.target.value as Vendor;
            setNewVendor(nextVendor);
            // Reset the platform to whatever the new vendor defaults to; users
            // can still switch to another platform in the same vendor family.
            setNewPlatform(defaultPlatformFor(nextVendor));
          }}
          disabled={isSubmitting}
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        >
          <option value="cisco_ios">Cisco IOS</option>
          <option value="huawei_vrp">Huawei VRP</option>
        </select>
        <select
          value={newPlatform}
          onChange={(e) => setNewPlatform(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        >
          {PLATFORMS_BY_VENDOR[newVendor].map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Username"
          value={newUsername}
          onChange={(e) => setNewUsername(e.target.value)}
          disabled={isSubmitting}
          required
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        />
        <select
          value={newAuthMethod}
          onChange={(e) => setNewAuthMethod(e.target.value as AuthMethod)}
          disabled={isSubmitting}
          aria-label="Auth method"
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
        >
          <option value="password">Password</option>
          <option value="key">SSH Key</option>
        </select>
        {newAuthMethod === 'password' ? (
          <input
            type="password"
            placeholder="Password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            disabled={isSubmitting}
            required
            className="border border-panel-border rounded-md px-3 py-1.5 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        ) : (
          <textarea
            placeholder="Private key (PEM / OpenSSH)"
            value={newPrivateKey}
            onChange={(e) => setNewPrivateKey(e.target.value)}
            disabled={isSubmitting}
            required
            rows={2}
            className="border border-panel-border rounded-md px-3 py-1.5 text-xs font-mono w-56 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
          />
        )}
        <select
          value={newSiteId}
          onChange={(e) => {
            setNewSiteId(e.target.value);
            setNewGroupId(''); // will be repopulated when groups load
          }}
          disabled={isSubmitting}
          aria-label="Site"
          required
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
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
          className="border border-panel-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
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
            !newUsername.trim() || !newSiteId ||
            (newAuthMethod === 'password' ? !newPassword.trim() : !newPrivateKey.trim())
          }
          className="px-3 py-1.5 text-sm bg-info text-white rounded-md hover:bg-info disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isSubmitting && deletingDeviceName === null && editingDeviceName === null && !movingDevice ? 'Creating...' : 'Add Device'}
        </button>
      </form>
      )}

      {successMessage && (
        <div className="mb-4 text-sm text-success">&#10003; {successMessage}</div>
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
            className="mt-3 px-3 py-1.5 text-sm bg-panel border border-panel-border rounded-md hover:bg-panel-elev/60"
          >
            Retry
          </button>
        </div>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-panel-border bg-panel-elev/60">
              <th className="text-left px-4 py-2 font-medium text-text">Name</th>
              <th className="text-left px-4 py-2 font-medium text-text">Host</th>
              <th className="text-left px-4 py-2 font-medium text-text">Vendor</th>
              <th className="text-left px-4 py-2 font-medium text-text">Platform</th>
              <th className="text-left px-4 py-2 font-medium text-text">Username</th>
              <th className="text-left px-4 py-2 font-medium text-text">Auth</th>
              <th className="text-left px-4 py-2 font-medium text-text">Site</th>
              <th className="text-left px-4 py-2 font-medium text-text">Group</th>
              <th className="text-left px-4 py-2 font-medium text-text">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visibleDevices && visibleDevices.length > 0 ? (
              visibleDevices.map((device) => {
                const isEditing = editingDeviceName === device.name;
                return (
                  <tr key={device.id} className="border-b border-panel-border hover:bg-panel-elev/60">
                    <td className="px-4 py-2 text-text font-mono text-xs">{device.name}</td>
                    <td className="px-4 py-2 text-text">
                      {isEditing ? (
                        <input
                          type="text"
                          value={editingHost}
                          onChange={(e) => setEditingHost(e.target.value)}
                          disabled={isSubmitting}
                          autoFocus
                          className="border border-panel-border rounded-md px-2 py-1 text-sm w-36 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        />
                      ) : (
                        device.host
                      )}
                    </td>
                    <td className="px-4 py-2 text-text">
                      {isEditing ? (
                        <select
                          value={editingVendor}
                          onChange={(e) => {
                            const nextVendor = e.target.value as Vendor;
                            setEditingVendor(nextVendor);
                            setEditingPlatform(defaultPlatformFor(nextVendor));
                          }}
                          disabled={isSubmitting}
                          className="border border-panel-border rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        >
                          <option value="cisco_ios">Cisco IOS</option>
                          <option value="huawei_vrp">Huawei VRP</option>
                        </select>
                      ) : (
                        VENDOR_LABELS[device.vendor] ?? device.vendor
                      )}
                    </td>
                    <td className="px-4 py-2 text-text">
                      {isEditing ? (
                        <select
                          value={editingPlatform}
                          onChange={(e) => setEditingPlatform(e.target.value)}
                          disabled={isSubmitting}
                          className="border border-panel-border rounded-md px-2 py-1 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        >
                          {PLATFORMS_BY_VENDOR[editingVendor].map((p) => (
                            <option key={p.value} value={p.value}>
                              {p.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        device.platform
                      )}
                    </td>
                    <td className="px-4 py-2 text-text">
                      {isEditing ? (
                        <input
                          type="text"
                          value={editingUsername}
                          onChange={(e) => setEditingUsername(e.target.value)}
                          disabled={isSubmitting}
                          className="border border-panel-border rounded-md px-2 py-1 text-sm w-28 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        />
                      ) : (
                        device.username
                      )}
                    </td>
                    <td className="px-4 py-2 text-text">
                      {isEditing ? (
                        <select
                          value={editingAuthMethod}
                          onChange={(e) => setEditingAuthMethod(e.target.value as AuthMethod)}
                          disabled={isSubmitting}
                          aria-label="Auth method"
                          className="border border-panel-border rounded-md px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                        >
                          <option value="password">Password</option>
                          <option value="key">SSH Key</option>
                        </select>
                      ) : (
                        <span className={`text-xs px-1.5 py-0.5 rounded ${device.auth_method === 'key' ? 'bg-info/10 text-info' : 'bg-panel-elev/60 text-muted'}`}>
                          {device.auth_method === 'key' ? 'Key' : 'Password'}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-text">{device.site_name}</td>
                    <td className="px-4 py-2 text-text">{device.device_group_name}</td>
                    <td className="px-4 py-2">
                      {canMutate && (isEditing ? (
                        <div className="flex flex-wrap gap-2 items-center">
                          {editingAuthMethod === 'password' ? (
                            <input
                              type="password"
                              placeholder="New password"
                              value={editingPassword}
                              onChange={(e) => setEditingPassword(e.target.value)}
                              disabled={isSubmitting}
                              className="border border-panel-border rounded-md px-2 py-1 text-sm w-32 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                            />
                          ) : (
                            <textarea
                              placeholder="New private key"
                              value={editingPrivateKey}
                              onChange={(e) => setEditingPrivateKey(e.target.value)}
                              disabled={isSubmitting}
                              rows={2}
                              className="border border-panel-border rounded-md px-2 py-1 text-xs font-mono w-48 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
                            />
                          )}
                          <button
                            onClick={handleUpdate}
                            disabled={
                              isSubmitting ||
                              !editingHost.trim() || !editingPlatform.trim() || !editingUsername.trim()
                            }
                            className="px-2 py-1 text-xs text-white bg-info border border-blue-600 rounded hover:bg-info disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {isSubmitting && deletingDeviceName === null ? 'Saving...' : 'Save'}
                          </button>
                          <button
                            onClick={handleEditCancel}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-muted border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <div className="flex gap-2">
                          <button
                            onClick={() => handleEditStart(device)}
                            disabled={isSubmitting || editingDeviceName !== null}
                            className="px-2 py-1 text-xs text-info border border-info/40 rounded hover:bg-info/10 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Edit
                          </button>
                          <button
                            onClick={() => handleMoveStart(device)}
                            disabled={isSubmitting || editingDeviceName !== null}
                            className="px-2 py-1 text-xs text-info border border-info/40 rounded hover:bg-info/10 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            Move…
                          </button>
                          <button
                            onClick={() => handleDelete(device)}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-danger border border-danger/40 rounded hover:bg-danger/10 disabled:opacity-50 disabled:cursor-not-allowed"
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
                <td colSpan={9} className="px-4 py-8 text-center text-muted/70">
                  {scopedSiteId != null
                    ? (
                        <>
                          No devices in{' '}
                          <strong>{scopedSiteName ?? 'this site'}</strong>.
                          Switch the topbar scope to <em>All sites</em> to see
                          the rest of the inventory.
                        </>
                      )
                    : 'No devices registered yet.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {/* ── Move dialog ────────────────────────────────────────────────── */}
      {movingDevice && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-md bg-panel rounded-lg shadow-xl p-6">
            <h2 className="text-lg font-semibold text-text mb-1">
              Move device
            </h2>
            <p className="text-sm text-muted mb-4">
              <span className="font-mono">{movingDevice.name}</span> is currently
              in <span className="font-medium">{movingDevice.site_name}</span> /{' '}
              <span className="font-medium">{movingDevice.device_group_name}</span>.
              Pick a target site and group. Cross-site moves require admin
              on both sites (backend enforces).
            </p>
            <label className="block text-xs font-medium text-text mb-1">
              Target site
            </label>
            <select
              value={moveTargetSiteId}
              onChange={(e) => handleMoveTargetSiteChange(e.target.value)}
              disabled={isSubmitting}
              className="w-full border border-panel-border rounded-md px-3 py-1.5 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
            >
              {siteList.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}{s.id === movingDevice.site_id ? ' (current)' : ''}
                </option>
              ))}
            </select>
            <label className="block text-xs font-medium text-text mb-1">
              Target group
            </label>
            <select
              value={moveTargetGroupId}
              onChange={(e) => setMoveTargetGroupId(e.target.value)}
              disabled={isSubmitting}
              className="w-full border border-panel-border rounded-md px-3 py-1.5 text-sm mb-4 focus:outline-none focus:ring-2 focus:ring-info disabled:opacity-50"
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
                  setMoveTargetSiteId(String(movingDevice.site_id));
                  setMoveTargetGroupId('');
                }}
                disabled={isSubmitting}
                title="Send device to its current site's Default group (D8 shortcut)"
                className="px-3 py-1.5 text-xs text-info border border-info/40 rounded hover:bg-info/10 disabled:opacity-50"
              >
                Reset to Default
              </button>
              <div className="flex gap-2">
                <button
                  onClick={handleMoveCancel}
                  disabled={isSubmitting}
                  className="px-3 py-1.5 text-sm text-text border border-panel-border rounded hover:bg-panel-elev/60 disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleMoveConfirm}
                  disabled={isSubmitting}
                  className="px-3 py-1.5 text-sm text-white bg-info rounded hover:bg-info disabled:opacity-50"
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
