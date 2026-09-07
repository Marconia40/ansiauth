'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  addSVIDhcpRelay,
  batchUpdateSvi,
  removeSVIDhcpRelay,
} from '@/services/api';
import type { SVIBatchRequest } from '@/types/svi';
import type { SviRow } from './scopeSvis';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
  extractMessage,
} from './VlanCreateModal';
import { invalidateSviQueries } from './SVICreateModal';

type Tab = 'general' | 'ipv4' | 'ipv6' | 'acl' | 'dhcp';

interface Props {
  open: boolean;
  onClose: () => void;
  /** The SVI row to edit. When null the modal renders nothing (parent gates it). */
  row: SviRow | null;
  onDone?: (msg: string, tone: 'ok' | 'error') => void;
}

/** Editor de SVI. Una fila = un (device, vlan_id) unico, asi que el modal
 * opera sobre un solo device. Tabs internas para agrupar campos por dominio
 * (General / IPv4 / IPv6 / ACL / DHCP Relay); el save es global, calcula el
 * delta sobre TODAS las tabs y manda 1 sola llamada a `PATCH .../batch` con
 * todos los campos que cambiaron -- antes eran N llamadas en serie (1 por
 * campo, N conexiones SSH); ahora el server las junta en 1 sola conexión.
 * DHCP relay queda afuera de ese `PATCH .../batch` (el server la sigue
 * tratando como incremental, `POST`/`DELETE .../dhcp-relay`, 1 conexión por
 * server) pero ya NO dispara esas llamadas al tocar +/- -- se encola
 * localmente como el resto de las tabs y recién sale en el mismo Save. */
export function SVIEditModal({ open, onClose, row, onDone }: Props) {
  const queryClient = useQueryClient();

  const [tab, setTab] = useState<Tab>('general');

  // General
  const [adminUp, setAdminUp] = useState<boolean | null>(null);
  const [description, setDescription] = useState('');

  // IPv4
  const [ipv4Primary, setIpv4Primary] = useState('');
  const [ipv4Secondary, setIpv4Secondary] = useState('');

  // IPv6
  const [ipv6, setIpv6] = useState('');

  // ACL
  const [aclIn, setAclIn] = useState('');
  const [aclOut, setAclOut] = useState('');

  // DHCP relay — los endpoints siguen siendo incrementales (POST/DELETE 1
  // servidor a la vez, ver docstring del componente), pero +/- ahora solo
  // editan esta lista local; el delta contra `initial.dhcpRelayServers`
  // (`dhcpDelta` mas abajo) es lo que se manda, recien al hacer Save.
  const [dhcpServers, setDhcpServers] = useState<string[]>([]);
  const [dhcpDraft, setDhcpDraft] = useState('');
  const [dhcpAdding, setDhcpAdding] = useState(false);

  const [error, setError] = useState<string | null>(null);

  // Re-hidrata el form cada vez que cambia la row objetivo (o se abre el modal).
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!row) return;
    setTab('general');
    setAdminUp(row.adminUp);
    setDescription(row.description ?? '');
    setIpv4Primary(row.ipv4 ?? '');
    setIpv4Secondary(row.ipv4Secondary ?? '');
    setIpv6(row.ipv6 ?? '');
    setAclIn(row.aclIn ?? '');
    setAclOut(row.aclOut ?? '');
    setDhcpServers(row.dhcpRelayServers.slice());
    setDhcpDraft('');
    setDhcpAdding(false);
    setError(null);
  }, [row, open]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Snapshot inicial estable para dirty-check. Reseteado por el useEffect de arriba.
  const initial = row;

  // Delta de campos cambiados -> 1 solo body para PATCH .../batch (antes
  // eran N llamadas individuales en serie, ver docstring del componente).
  // `''` limpia el campo, mismo significado que ya tenía en cada endpoint
  // individual -- no se inventa nada nuevo, solo se junta en 1 objeto.
  const pending = useMemo<{ body: SVIBatchRequest; labels: string[] }>(() => {
    if (!initial) return { body: {}, labels: [] };
    const body: SVIBatchRequest = {};
    const labels: string[] = [];

    const descNext = description.trim();
    if (descNext !== (initial.description ?? '')) {
      body.description = descNext;
      labels.push(descNext === '' ? 'clear description' : 'set description');
    }

    // Admin state — solo si cambio y no es null (null = desconocido, no editable).
    if (adminUp !== null && adminUp !== initial.adminUp) {
      body.admin_up = adminUp;
      labels.push(`admin ${adminUp ? 'up' : 'down'}`);
    }

    const p4Next = ipv4Primary.trim();
    if (p4Next !== (initial.ipv4 ?? '')) {
      body.ipv4_address = p4Next;
      labels.push(p4Next === '' ? 'clear IPv4 primary' : 'set IPv4 primary');
    }

    const s4Next = ipv4Secondary.trim();
    if (s4Next !== (initial.ipv4Secondary ?? '')) {
      body.ipv4_address_secondary = s4Next;
      labels.push(s4Next === '' ? 'clear IPv4 secondary' : 'set IPv4 secondary');
    }

    const v6Next = ipv6.trim();
    if (v6Next !== (initial.ipv6 ?? '')) {
      body.ipv6_address = v6Next;
      labels.push(v6Next === '' ? 'clear IPv6' : 'set IPv6');
    }

    const aInNext = aclIn.trim();
    if (aInNext !== (initial.aclIn ?? '')) {
      body.acl_in = aInNext;
      labels.push(aInNext === '' ? 'clear ACL in' : 'set ACL in');
    }

    const aOutNext = aclOut.trim();
    if (aOutNext !== (initial.aclOut ?? '')) {
      body.acl_out = aOutNext;
      labels.push(aOutNext === '' ? 'clear ACL out' : 'set ACL out');
    }

    return { body, labels };
  }, [
    initial,
    description,
    adminUp,
    ipv4Primary,
    ipv4Secondary,
    ipv6,
    aclIn,
    aclOut,
  ]);

  // Delta de DHCP relay — diff local `dhcpServers` (editado por +/-, sin
  // red) contra `initial.dhcpRelayServers`. Mismo criterio que `pending`
  // arriba: se calcula, no se dispara, hasta el Save.
  const dhcpDelta = useMemo(() => {
    if (!initial) return { adds: [] as string[], removes: [] as string[] };
    const originalSet = new Set(initial.dhcpRelayServers);
    const currentSet = new Set(dhcpServers);
    return {
      adds: dhcpServers.filter((s) => !originalSet.has(s)),
      removes: initial.dhcpRelayServers.filter((s) => !currentSet.has(s)),
    };
  }, [initial, dhcpServers]);

  const totalChanges = pending.labels.length + dhcpDelta.adds.length + dhcpDelta.removes.length;

  const dirtyByTab = useMemo(() => {
    if (!initial) {
      return { general: false, ipv4: false, ipv6: false, acl: false, dhcp: false };
    }
    return {
      general:
        description.trim() !== (initial.description ?? '') ||
        (adminUp !== null && adminUp !== initial.adminUp),
      ipv4:
        ipv4Primary.trim() !== (initial.ipv4 ?? '') ||
        ipv4Secondary.trim() !== (initial.ipv4Secondary ?? ''),
      ipv6: ipv6.trim() !== (initial.ipv6 ?? ''),
      acl:
        aclIn.trim() !== (initial.aclIn ?? '') ||
        aclOut.trim() !== (initial.aclOut ?? ''),
      dhcp: dhcpDelta.adds.length > 0 || dhcpDelta.removes.length > 0,
    };
  }, [
    initial,
    description,
    adminUp,
    ipv4Primary,
    ipv4Secondary,
    ipv6,
    aclIn,
    aclOut,
    dhcpDelta,
  ]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!initial) return;
      if (Object.keys(pending.body).length > 0) {
        await batchUpdateSvi(initial.device, initial.vlanId, pending.body);
      }
      // DHCP relay sigue siendo 1 request por server (server no soporta
      // full-replace) -- secuencial, no Promise.all: cada llamada hace su
      // propio read-modify-write de la lista completa, en paralelo se
      // pisarían entre si.
      for (const s of dhcpDelta.removes) {
        await removeSVIDhcpRelay(initial.device, { vlan_id: initial.vlanId, server: s });
      }
      for (const s of dhcpDelta.adds) {
        await addSVIDhcpRelay(initial.device, { vlan_id: initial.vlanId, server: s });
      }
    },
    onSuccess: () => {
      onDone?.(
        `SVI ${initial?.vlanId} on ${initial?.device}: ${totalChanges} change(s) applied.`,
        'ok',
      );
      onClose();
    },
    onSettled: () => invalidateSviQueries(queryClient),
    onError: (err: unknown) => setError(extractMessage(err, 'Save failed.')),
  });

  function queueAddDhcpServer() {
    const v = dhcpDraft.trim();
    if (!v) return;
    if (dhcpServers.includes(v)) {
      setDhcpDraft('');
      setDhcpAdding(false);
      return;
    }
    setDhcpServers([...dhcpServers, v]);
    setDhcpDraft('');
    setDhcpAdding(false);
  }

  function queueRemoveDhcpServer(s: string) {
    setDhcpServers(dhcpServers.filter((x) => x !== s));
  }

  const canSubmit = totalChanges > 0 && !mutation.isPending;

  if (!row) return null;

  return (
    <Modal
      open={open}
      onClose={() => (mutation.isPending ? undefined : onClose())}
      title={`Edit SVI — VLAN ${row.vlanId} on ${row.device}`}
      widthClass="w-full max-w-2xl"
      footer={
        <>
          <span className="text-xs text-muted mr-auto">
            {totalChanges === 0
              ? 'No changes'
              : `${totalChanges} pending change${totalChanges === 1 ? '' : 's'}`}
          </span>
          <ModalSecondary onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </ModalPrimary>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <TabBar
          tab={tab}
          onChange={setTab}
          dirty={dirtyByTab}
          disabled={mutation.isPending}
        />

        {tab === 'general' && (
          <GeneralTab
            adminUp={adminUp}
            onAdminUpChange={setAdminUp}
            initialAdminUp={initial?.adminUp ?? null}
            description={description}
            onDescriptionChange={setDescription}
          />
        )}
        {tab === 'ipv4' && (
          <Ipv4Tab
            primary={ipv4Primary}
            onPrimaryChange={setIpv4Primary}
            secondary={ipv4Secondary}
            onSecondaryChange={setIpv4Secondary}
            initialPrimary={initial?.ipv4 ?? ''}
          />
        )}
        {tab === 'ipv6' && (
          <Ipv6Tab value={ipv6} onChange={setIpv6} />
        )}
        {tab === 'acl' && (
          <AclTab
            aclIn={aclIn}
            onAclInChange={setAclIn}
            aclOut={aclOut}
            onAclOutChange={setAclOut}
          />
        )}
        {tab === 'dhcp' && (
          <DhcpTab
            servers={dhcpServers}
            draft={dhcpDraft}
            onDraftChange={setDhcpDraft}
            adding={dhcpAdding}
            onAddingChange={setDhcpAdding}
            onAdd={queueAddDhcpServer}
            onRemove={queueRemoveDhcpServer}
          />
        )}

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2 whitespace-pre-wrap">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}

// ── Tabs ─────────────────────────────────────────────────────────────────────

function TabBar({
  tab,
  onChange,
  dirty,
  disabled,
}: {
  tab: Tab;
  onChange: (t: Tab) => void;
  dirty: Record<Tab, boolean>;
  disabled?: boolean;
}) {
  const items: Array<{ key: Tab; label: string }> = [
    { key: 'general', label: 'General' },
    { key: 'ipv4', label: 'IPv4' },
    { key: 'ipv6', label: 'IPv6' },
    { key: 'acl', label: 'ACL' },
    { key: 'dhcp', label: 'DHCP Relay' },
  ];
  return (
    <div className="flex gap-1 border-b border-panel-border">
      {items.map((it) => {
        const active = tab === it.key;
        return (
          <button
            key={it.key}
            type="button"
            disabled={disabled}
            onClick={() => onChange(it.key)}
            className={`relative px-3 py-1.5 text-xs font-semibold uppercase tracking-wider transition-colors -mb-px border-b-2 ${
              active
                ? 'text-info border-info'
                : 'text-muted border-transparent hover:text-text'
            }`}
          >
            {it.label}
            {dirty[it.key] && (
              <span
                className="ml-1 inline-block h-1.5 w-1.5 rounded-full bg-warning align-middle"
                title="Unsaved changes"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

function GeneralTab({
  adminUp,
  onAdminUpChange,
  initialAdminUp,
  description,
  onDescriptionChange,
}: {
  adminUp: boolean | null;
  onAdminUpChange: (v: boolean) => void;
  initialAdminUp: boolean | null;
  description: string;
  onDescriptionChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <FieldRow label="Description">
        <input
          type="text"
          value={description}
          onChange={(e) => onDescriptionChange(e.target.value)}
          placeholder="Leave empty to clear"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
        />
        <p className="text-xs text-muted mt-1">
          Clearing the field and saving will DELETE the description on the device.
        </p>
      </FieldRow>

      <FieldRow label="Admin state">
        <div className="flex items-center gap-2">
          <SegButton
            active={adminUp === true}
            onClick={() => onAdminUpChange(true)}
            tone="ok"
          >
            Up
          </SegButton>
          <SegButton
            active={adminUp === false}
            onClick={() => onAdminUpChange(false)}
            tone="danger"
          >
            Down
          </SegButton>
          {initialAdminUp === null && (
            <span className="text-xs text-muted italic">
              current state unknown — pick one to set
            </span>
          )}
        </div>
      </FieldRow>
    </div>
  );
}

function Ipv4Tab({
  primary,
  onPrimaryChange,
  secondary,
  onSecondaryChange,
  initialPrimary,
}: {
  primary: string;
  onPrimaryChange: (v: string) => void;
  secondary: string;
  onSecondaryChange: (v: string) => void;
  initialPrimary: string;
}) {
  const secondaryBlocked = primary.trim() === '' && secondary.trim() !== '';
  return (
    <div className="flex flex-col gap-4">
      <FieldRow label="Primary address (CIDR)">
        <input
          type="text"
          value={primary}
          onChange={(e) => onPrimaryChange(e.target.value)}
          placeholder="e.g. 10.10.10.11/24 — empty to clear"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info font-mono"
        />
      </FieldRow>

      <FieldRow label="Secondary address (CIDR)">
        <input
          type="text"
          value={secondary}
          onChange={(e) => onSecondaryChange(e.target.value)}
          placeholder="Optional — requires a primary already configured"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info font-mono"
        />
        {secondaryBlocked && (
          <p className="text-xs text-danger mt-1">
            The device rejects a secondary without a primary. Save will fail
            if you clear the primary while keeping a secondary.
          </p>
        )}
        {initialPrimary === '' && secondary.trim() !== '' && primary.trim() !== '' && (
          <p className="text-xs text-muted mt-1">
            Primary is being set for the first time — the secondary edit is
            queued after it, so the order matters (both fire in sequence).
          </p>
        )}
      </FieldRow>
    </div>
  );
}

function Ipv6Tab({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <FieldRow label="IPv6 address (CIDR)">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. 2001:db8::1/64 — empty to clear"
        className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info font-mono"
      />
    </FieldRow>
  );
}

function AclTab({
  aclIn,
  onAclInChange,
  aclOut,
  onAclOutChange,
}: {
  aclIn: string;
  onAclInChange: (v: string) => void;
  aclOut: string;
  onAclOutChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-4">
      <FieldRow label="ACL name — inbound">
        <input
          type="text"
          value={aclIn}
          onChange={(e) => onAclInChange(e.target.value)}
          placeholder="ACL must already exist on the device — empty to unbind"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info font-mono"
        />
      </FieldRow>
      <FieldRow label="ACL name — outbound">
        <input
          type="text"
          value={aclOut}
          onChange={(e) => onAclOutChange(e.target.value)}
          placeholder="ACL must already exist on the device — empty to unbind"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info font-mono"
        />
      </FieldRow>
    </div>
  );
}

function DhcpTab({
  servers,
  draft,
  onDraftChange,
  adding,
  onAddingChange,
  onAdd,
  onRemove,
}: {
  servers: string[];
  draft: string;
  onDraftChange: (v: string) => void;
  adding: boolean;
  onAddingChange: (v: boolean) => void;
  onAdd: () => void;
  onRemove: (s: string) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-md border border-panel-border p-3 flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-wider text-muted mb-1">
          DHCP relay servers
        </p>

        {servers.map((s) => (
          <div key={s} className="flex items-center gap-2">
            <div className="flex-1 rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text font-mono">
              {s}
            </div>
            <button
              type="button"
              onClick={() => onRemove(s)}
              aria-label={`Remove relay ${s}`}
              className="rounded-md border border-danger/50 text-danger px-3 py-2 text-sm leading-none hover:bg-danger/10 transition"
            >
              −
            </button>
          </div>
        ))}

        {adding ? (
          <div className="flex items-center gap-2">
            <input
              type="text"
              autoFocus
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  onAdd();
                } else if (e.key === 'Escape') {
                  e.preventDefault();
                  onDraftChange('');
                  onAddingChange(false);
                }
              }}
              placeholder="e.g. 10.0.0.53"
              className="flex-1 rounded-md bg-panel-elev border border-info px-3 py-2 text-sm text-text font-mono focus:outline-none focus:ring-2 focus:ring-info"
            />
            <button
              type="button"
              onClick={onAdd}
              disabled={draft.trim() === ''}
              className="rounded-md border border-info text-info px-3 py-2 text-xs font-semibold uppercase tracking-wider hover:bg-info/10 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              Add
            </button>
            <button
              type="button"
              onClick={() => {
                onDraftChange('');
                onAddingChange(false);
              }}
              className="rounded-md border border-panel-border text-muted px-3 py-2 text-xs font-semibold uppercase tracking-wider hover:text-text transition"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => onAddingChange(true)}
            className="w-full rounded-md border-2 border-dashed border-panel-border px-3 py-2 text-sm text-muted hover:text-text hover:border-muted transition"
          >
            + Add server
          </button>
        )}
      </div>

      <p className="text-xs text-muted italic">
        Queued locally — applied together when you click Save, same as the
        other tabs (still 1 request per server, not combined into a single
        connection).
      </p>
    </div>
  );
}

function SegButton({
  active,
  onClick,
  tone,
  children,
}: {
  active: boolean;
  onClick: () => void;
  tone: 'ok' | 'danger';
  children: React.ReactNode;
}) {
  const activeCls =
    tone === 'ok'
      ? 'bg-success/20 text-success border-success/50'
      : 'bg-danger/20 text-danger border-danger/50';
  const idleCls =
    'bg-panel-elev text-muted border-panel-border hover:text-text';
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-md border px-3 py-1 text-xs font-semibold uppercase tracking-wider transition ${
        active ? activeCls : idleCls
      }`}
    >
      {children}
    </button>
  );
}
