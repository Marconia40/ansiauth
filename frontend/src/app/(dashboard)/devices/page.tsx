'use client';

import { useState } from 'react';
import { useAuth } from '@/context/AuthContext';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { getDevices, createDevice, updateDevice, deleteDevice } from '@/services/api';
import type { Device, DeviceUpdate, Vendor } from '@/types/device';

function extractMessage(error: unknown, fallback: string): string {
  const e = error as { response?: { data?: { detail?: string; message?: string } }; message?: string } | null;
  return e?.response?.data?.detail ?? e?.response?.data?.message ?? e?.message ?? fallback;
}

const VENDOR_LABELS: Record<Vendor, string> = {
  cisco: 'Cisco IOS',
  huawei: 'Huawei',
};

export default function DevicesPage() {
  const { user } = useAuth();
  const canMutate = !!user && user.role !== 'observer';

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // Create form
  const [newName, setNewName] = useState('');
  const [newHost, setNewHost] = useState('');
  const [newVendor, setNewVendor] = useState<Vendor>('cisco');
  const [newPlatform, setNewPlatform] = useState('');
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');

  // Edit state
  const [editingDeviceName, setEditingDeviceName] = useState<string | null>(null);
  const [editingHost, setEditingHost] = useState('');
  const [editingVendor, setEditingVendor] = useState<Vendor>('cisco');
  const [editingPlatform, setEditingPlatform] = useState('');
  const [editingUsername, setEditingUsername] = useState('');
  const [editingPassword, setEditingPassword] = useState('');

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

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (
      !newName.trim() || !newHost.trim() || !newPlatform.trim() ||
      !newUsername.trim() || !newPassword.trim()
    ) return;
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
      });
      setNewName('');
      setNewHost('');
      setNewVendor('cisco');
      setNewPlatform('');
      setNewUsername('');
      setNewPassword('');
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
    if (!editingHost.trim() || !editingPlatform.trim() || !editingUsername.trim()) return;
    setIsSubmitting(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    const deviceName = editingDeviceName!;
    try {
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
      setEditingDeviceName(null);
      setEditingHost('');
      setEditingVendor('cisco');
      setEditingPlatform('');
      setEditingUsername('');
      setEditingPassword('');
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
        <button
          type="submit"
          disabled={
            isSubmitting ||
            !newName.trim() || !newHost.trim() || !newPlatform.trim() ||
            !newUsername.trim() || !newPassword.trim()
          }
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {isSubmitting ? 'Creating...' : 'Add Device'}
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
                            {isSubmitting ? 'Saving...' : 'Save'}
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
                            onClick={() => handleDelete(device)}
                            disabled={isSubmitting}
                            className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                          >
                            {isSubmitting ? 'Deleting...' : 'Delete'}
                          </button>
                        </div>
                      ))}
                    </td>
                  </tr>
                );
              })
            ) : (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-gray-400">
                  No devices registered.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
