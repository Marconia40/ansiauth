'use client';

import { useState, useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { PageHeader } from '@/components/PageHeader';
import { LoadingSpinner } from '@/components/LoadingSpinner';
import { ErrorMessage } from '@/components/ErrorMessage';
import { useHasRole } from '@/components/RequireRole';
import {
  getDeviceGroups,
  getDevices,
  createDeviceGroup,
  deleteDeviceGroup,
  getDeviceGroupDevices,
  moveDevice,
  getSites,
  extractMessage,
} from '@/services/api';
import type { DeviceGroup } from '@/services/api';
import type { Device } from '@/types/device';
import type { Site } from '@/types/site';

export default function DeviceGroupsPage() {
  const canMutate = useHasRole('operator');

  const [newGroupName, setNewGroupName] = useState('');
  const [newGroupDescription, setNewGroupDescription] = useState('');
  const [newGroupSiteId, setNewGroupSiteId] = useState<string>('');
  const [isCreating, setIsCreating] = useState(false);

  const [addingToGroup, setAddingToGroup] = useState<number | null>(null);
  const [removingMember, setRemovingMember] = useState<string | null>(null);
  const [deletingGroupId, setDeletingGroupId] = useState<number | null>(null);

  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [groupMembers, setGroupMembers] = useState<Record<number, string[]>>({});
  const [selectedDevice, setSelectedDevice] = useState<Record<number, string>>({});

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
    data: groups,
    isLoading: groupsLoading,
    isFetching: groupsFetching,
    error: groupsError,
    refetch: refetchGroups,
  } = useQuery<DeviceGroup[]>({
    queryKey: ['device-groups'],
    queryFn: getDeviceGroups,
  });

  const {
    data: devices,
    isLoading: devicesLoading,
    isFetching: devicesFetching,
    error: devicesError,
    refetch: refetchDevices,
  } = useQuery<Device[]>({
    queryKey: ['devices'],
    queryFn: getDevices,
  });

  const { data: sites } = useQuery<Site[]>({ queryKey: ['sites'], queryFn: getSites });
  const siteList = sites ?? [];

  const isFetching = groupsFetching || devicesFetching;
  const isLoading = groupsLoading || devicesLoading;
  const isAnyOperationRunning = isCreating || !!addingToGroup || !!removingMember || !!deletingGroupId;

  useEffect(() => {
    if (!groups || groups.length === 0) {
      setGroupMembers({});
      return;
    }
    Promise.all(
      groups.map((g) =>
        getDeviceGroupDevices(g.id)
          .then((members) => ({ id: g.id, members }))
          .catch(() => ({ id: g.id, members: [] as string[] })),
      ),
    ).then((results) => {
      const map: Record<number, string[]> = {};
      results.forEach(({ id, members }) => {
        map[id] = members;
      });
      setGroupMembers(map);
    });
  }, [groups]);

  function handleRefresh() {
    refetchGroups();
    refetchDevices();
  }

  async function refreshGroupMembers(groupId: number) {
    try {
      const members = await getDeviceGroupDevices(groupId);
      setGroupMembers((prev) => ({ ...prev, [groupId]: members }));
    } catch {
      // non-fatal
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newGroupName.trim()) { setErrorMessage('Group name is required'); return; }
    if (!newGroupSiteId) { setErrorMessage('Site is required'); return; }
    setIsCreating(true);
    setSuccessMessage(null);
    setErrorMessage(null);
    const groupName = newGroupName.trim();
    try {
      await createDeviceGroup({
        name: groupName,
        description: newGroupDescription.trim() || undefined,
        site_id: Number(newGroupSiteId),
      });
      setNewGroupName('');
      setNewGroupDescription('');
      setNewGroupSiteId('');
      await refetchGroups();
      setSuccessMessage(`Device group ${groupName} created successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Create failed'));
    } finally {
      setIsCreating(false);
    }
  }

  async function handleDeleteGroup(group: DeviceGroup) {
    if (!window.confirm(`Delete device group ${group.name}?`)) return;
    setDeletingGroupId(group.id);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await deleteDeviceGroup(group.id);
      setGroupMembers((prev) => {
        const next = { ...prev };
        delete next[group.id];
        return next;
      });
      await refetchGroups();
      setSuccessMessage(`Device group ${group.name} deleted successfully`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Delete failed'));
    } finally {
      setDeletingGroupId(null);
    }
  }

  async function handleAddDevice(group: DeviceGroup) {
    const deviceName = selectedDevice[group.id];
    if (!deviceName) return;
    setAddingToGroup(group.id);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      await moveDevice(deviceName, group.id);
      setSelectedDevice((prev) => ({ ...prev, [group.id]: '' }));
      await refreshGroupMembers(group.id);
      await refetchGroups();
      setSuccessMessage(`Device ${deviceName} added to group ${group.name}`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Add failed'));
    } finally {
      setAddingToGroup(null);
    }
  }

  async function handleRemoveDevice(group: DeviceGroup, deviceName: string) {
    if (!window.confirm(`Remove ${deviceName} from ${group.name}?`)) return;
    const key = `${group.id}-${deviceName}`;
    setRemovingMember(key);
    setSuccessMessage(null);
    setErrorMessage(null);
    try {
      // Per D8, "remove from group" is a device-level move to the site's
      // Default group — pass ``null`` and the backend resolves it.
      await moveDevice(deviceName, null);
      await refreshGroupMembers(group.id);
      await refetchGroups();
      setSuccessMessage(`Device ${deviceName} removed from group`);
    } catch (err) {
      setErrorMessage(extractMessage(err, 'Remove failed'));
    } finally {
      setRemovingMember(null);
    }
  }

  if (isLoading) {
    return (
      <div className="flex justify-center py-24">
        <LoadingSpinner size="lg" />
      </div>
    );
  }

  const firstError = groupsError || devicesError;
  if (firstError) {
    return (
      <div className="py-8">
        <ErrorMessage error={extractMessage(firstError, 'Could not load data')} />
        <button
          onClick={handleRefresh}
          className="mt-3 px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50"
        >
          Retry
        </button>
      </div>
    );
  }

  const groupList = groups ?? [];
  const deviceList = devices ?? [];

  return (
    <div>
      <PageHeader
        title="Device Groups"
        actions={
          <button
            onClick={handleRefresh}
            disabled={isFetching || isAnyOperationRunning}
            className="px-3 py-1.5 text-sm bg-white border border-gray-300 rounded-md hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isFetching ? 'Refreshing...' : 'Refresh'}
          </button>
        }
      />
      <p className="text-sm text-gray-500 mb-6">Manage logical device groupings</p>

      {canMutate && (
        <div className="border border-gray-200 rounded-md p-4 mb-6">
          <h2 className="text-sm font-semibold text-gray-700 mb-3">Create Group</h2>
          <form onSubmit={handleCreate} className="flex flex-wrap gap-2 items-center">
            <input
              type="text"
              placeholder="Group name"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              disabled={isCreating}
              required
              className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-44 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            />
            <input
              type="text"
              placeholder="Description (optional)"
              value={newGroupDescription}
              onChange={(e) => setNewGroupDescription(e.target.value)}
              disabled={isCreating}
              className="border border-gray-300 rounded-md px-3 py-1.5 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            />
            <select
              value={newGroupSiteId}
              onChange={(e) => setNewGroupSiteId(e.target.value)}
              disabled={isCreating || siteList.length === 0}
              required
              aria-label="Site"
              className="border border-gray-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            >
              <option value="">{siteList.length === 0 ? 'No sites available' : 'Select site...'}</option>
              {siteList.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
            <button
              type="submit"
              disabled={isCreating || isAnyOperationRunning || !newGroupName.trim() || !newGroupSiteId}
              className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isCreating ? 'Creating...' : 'Create Group'}
            </button>
          </form>
          {siteList.length === 0 && (
            <p className="mt-2 text-xs text-gray-500">
              You need to create at least one site before you can create a device group.
            </p>
          )}
        </div>
      )}

      {successMessage && (
        <div className="mb-4 text-sm text-green-700">&#10003; {successMessage}</div>
      )}
      {errorMessage && (
        <div className="mb-4">
          <ErrorMessage error={`✗ ${errorMessage}`} />
        </div>
      )}

      {groupList.length === 0 ? (
        <p className="text-sm text-gray-400 py-8">No device groups created yet.</p>
      ) : (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-gray-200 bg-gray-50">
              <th className="text-left px-4 py-2 font-medium text-gray-700">Name</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Site</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Description</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Members</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700 whitespace-nowrap">Created</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Devices</th>
              <th className="text-left px-4 py-2 font-medium text-gray-700">Actions</th>
            </tr>
          </thead>
          <tbody>
            {groupList.map((group) => {
              const members = groupMembers[group.id] ?? [];
              const isAdding = addingToGroup === group.id;
              const isDeletingThis = deletingGroupId === group.id;
              const groupSelectedDevice = selectedDevice[group.id] ?? '';

              const groupDevicePool = deviceList.filter((d) => d.site_id === group.site_id);
              return (
                <tr key={group.id} className="border-b border-gray-100 hover:bg-gray-50 align-top">
                  <td className="px-4 py-3 text-gray-900 font-medium">{group.name}</td>
                  <td className="px-4 py-3 text-gray-700">
                    {group.site_name ?? <span className="text-amber-600 text-xs">unset (legacy)</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-600">{group.description ?? '—'}</td>
                  <td className="px-4 py-3 text-gray-600">{group.member_count}</td>
                  <td className="px-4 py-3 text-gray-600 whitespace-nowrap text-xs">
                    {new Date(group.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3">
                    {members.length === 0 ? (
                      <span className="text-gray-400 text-xs">No devices</span>
                    ) : (
                      <div className="flex flex-col gap-1 mb-2">
                        {members.map((device) => {
                          const removeKey = `${group.id}-${device}`;
                          const isRemoving = removingMember === removeKey;
                          return (
                            <div key={device} className="flex items-center gap-2">
                              <span className="font-mono text-xs text-gray-900">{device}</span>
                              {canMutate && (
                                <button
                                  onClick={() => handleRemoveDevice(group, device)}
                                  disabled={isRemoving || isDeletingThis || isAdding}
                                  className="px-1.5 py-0.5 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                                >
                                  {isRemoving ? 'Removing...' : 'Remove'}
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {canMutate && group.site_id !== null && (
                      <div className="flex gap-1 items-center mt-1">
                        <select
                          value={groupSelectedDevice}
                          onChange={(e) =>
                            setSelectedDevice((prev) => ({ ...prev, [group.id]: e.target.value }))
                          }
                          disabled={isAdding || isDeletingThis || groupDevicePool.length === 0}
                          className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:opacity-50"
                        >
                          <option value="">
                            {groupDevicePool.length === 0
                              ? `No devices in ${group.site_name ?? 'this site'}`
                              : 'Select device...'}
                          </option>
                          {groupDevicePool
                            .filter((d) => !members.includes(d.name))
                            .map((d) => (
                              <option key={d.name} value={d.name}>
                                {d.name}
                              </option>
                            ))}
                        </select>
                        <button
                          onClick={() => handleAddDevice(group)}
                          disabled={!groupSelectedDevice || isAdding || isDeletingThis}
                          className="px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {isAdding ? 'Adding...' : 'Add'}
                        </button>
                      </div>
                    )}
                    {canMutate && group.site_id === null && (
                      <p className="text-xs text-amber-600 mt-1">
                        Set this group&apos;s site before adding members
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {canMutate && (
                      <button
                        onClick={() => handleDeleteGroup(group)}
                        disabled={isDeletingThis || isAdding}
                        className="px-2 py-1 text-xs text-red-600 border border-red-300 rounded hover:bg-red-50 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {isDeletingThis ? 'Deleting...' : 'Delete'}
                      </button>
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
}
