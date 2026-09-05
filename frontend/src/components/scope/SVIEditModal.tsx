'use client';

import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  addSVIDhcpRelay,
  clearSVIAcl,
  clearSVIDescription,
  clearSVIIpv4,
  clearSVIIpv6,
  removeSVIDhcpRelay,
  setSVIAcl,
  setSVIAdminState,
  setSVIDescription,
  setSVIIpv4,
  setSVIIpv6,
} from '@/services/api';
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

/** Etiqueta legible por operacion, para poder decir en el toast que operaciones
 * fallaron cuando hay error parcial. */
interface Op {
  label: string;
  run: () => Promise<unknown>;
}

/** Editor de SVI. Una fila = un (device, vlan_id) unico, asi que el modal
 * opera sobre un solo device. Tabs internas para agrupar campos por dominio
 * (General / IPv4 / IPv6 / ACL / DHCP Relay); el save es global, calcula el
 * delta sobre TODAS las tabs y despacha solo los endpoints cuyo campo cambio.
 * Los endpoints se ejecutan en serie para no colisionar sobre el mismo device. */
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

  // DHCP relay — modelado aparte del resto: los endpoints son incrementales
  // (POST/DELETE 1 servidor a la vez), asi que cada fila dispara su propia
  // llamada en el momento y no participa del delta/Save global.
  const [dhcpServers, setDhcpServers] = useState<string[]>([]);
  const [dhcpDraft, setDhcpDraft] = useState('');
  const [dhcpAdding, setDhcpAdding] = useState(false);
  const [dhcpPending, setDhcpPending] = useState<string | null>(null);
  const [dhcpError, setDhcpError] = useState<string | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(
    null,
  );

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
    setDhcpPending(null);
    setDhcpError(null);
    setError(null);
    setProgress(null);
  }, [row, open]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // Snapshot inicial estable para dirty-check. Reseteado por el useEffect de arriba.
  const initial = row;

  const ops = useMemo<Op[]>(() => {
    if (!initial) return [];
    const out: Op[] = [];
    const device = initial.device;
    const vlan_id = initial.vlanId;

    // Description — trim para evitar ruido de espacios.
    const descNext = description.trim();
    const descInit = initial.description ?? '';
    if (descNext !== descInit) {
      if (descNext === '') {
        out.push({
          label: 'clear description',
          run: () => clearSVIDescription(device, { vlan_id }),
        });
      } else {
        out.push({
          label: 'set description',
          run: () => setSVIDescription(device, { vlan_id, description: descNext }),
        });
      }
    }

    // Admin state — solo si cambio y no es null (null = desconocido, no editable).
    if (adminUp !== null && adminUp !== initial.adminUp) {
      out.push({
        label: `admin ${adminUp ? 'up' : 'down'}`,
        run: () => setSVIAdminState(device, { vlan_id, enabled: adminUp }),
      });
    }

    // IPv4 primary
    const p4Next = ipv4Primary.trim();
    const p4Init = initial.ipv4 ?? '';
    if (p4Next !== p4Init) {
      if (p4Next === '') {
        out.push({
          label: 'clear IPv4 primary',
          run: () => clearSVIIpv4(device, { vlan_id, secondary: false }),
        });
      } else {
        out.push({
          label: 'set IPv4 primary',
          run: () =>
            setSVIIpv4(device, {
              vlan_id,
              ipv4_address: p4Next,
              secondary: false,
            }),
        });
      }
    }

    // IPv4 secondary
    const s4Next = ipv4Secondary.trim();
    const s4Init = initial.ipv4Secondary ?? '';
    if (s4Next !== s4Init) {
      if (s4Next === '') {
        out.push({
          label: 'clear IPv4 secondary',
          run: () => clearSVIIpv4(device, { vlan_id, secondary: true }),
        });
      } else {
        out.push({
          label: 'set IPv4 secondary',
          run: () =>
            setSVIIpv4(device, {
              vlan_id,
              ipv4_address: s4Next,
              secondary: true,
            }),
        });
      }
    }

    // IPv6
    const v6Next = ipv6.trim();
    const v6Init = initial.ipv6 ?? '';
    if (v6Next !== v6Init) {
      if (v6Next === '') {
        out.push({
          label: 'clear IPv6',
          run: () => clearSVIIpv6(device, { vlan_id }),
        });
      } else {
        out.push({
          label: 'set IPv6',
          run: () => setSVIIpv6(device, { vlan_id, ipv6_address: v6Next }),
        });
      }
    }

    // ACL in
    const aInNext = aclIn.trim();
    const aInInit = initial.aclIn ?? '';
    if (aInNext !== aInInit) {
      if (aInNext === '') {
        out.push({
          label: 'clear ACL in',
          run: () => clearSVIAcl(device, { vlan_id, direction: 'in' }),
        });
      } else {
        out.push({
          label: 'set ACL in',
          run: () =>
            setSVIAcl(device, { vlan_id, direction: 'in', acl_name: aInNext }),
        });
      }
    }

    // ACL out
    const aOutNext = aclOut.trim();
    const aOutInit = initial.aclOut ?? '';
    if (aOutNext !== aOutInit) {
      if (aOutNext === '') {
        out.push({
          label: 'clear ACL out',
          run: () => clearSVIAcl(device, { vlan_id, direction: 'out' }),
        });
      } else {
        out.push({
          label: 'set ACL out',
          run: () =>
            setSVIAcl(device, { vlan_id, direction: 'out', acl_name: aOutNext }),
        });
      }
    }

    // DHCP relay NO participa aca — su tab dispara POST/DELETE incremental
    // por fila en el momento del click.

    return out;
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
      // DHCP tab es modo inmediato — no acumula "dirty" para el Save global.
      dhcp: false,
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
  ]);

  const mutation = useMutation({
    mutationFn: async () => {
      setProgress({ done: 0, total: ops.length });
      const failures: Array<{ label: string; msg: string }> = [];
      let done = 0;
      // Serie: sobre el mismo device no queremos concurrentes que se pisen
      // en la sesion netconf/CLI subyacente.
      for (const op of ops) {
        try {
          await op.run();
        } catch (err) {
          failures.push({ label: op.label, msg: extractMessage(err, 'failed') });
        } finally {
          done += 1;
          setProgress({ done, total: ops.length });
        }
      }
      if (failures.length > 0) {
        const summary = failures
          .map((f) => `${f.label}: ${f.msg}`)
          .join('; ');
        throw new Error(
          `${failures.length} of ${ops.length} change(s) failed — ${summary}`,
        );
      }
    },
    onSuccess: () => {
      onDone?.(
        `SVI ${initial?.vlanId} on ${initial?.device}: ${ops.length} change(s) applied.`,
        'ok',
      );
      invalidateSviQueries(queryClient);
      onClose();
    },
    onError: (err: unknown) => setError(extractMessage(err, 'Save failed.')),
  });

  async function addDhcpServer() {
    if (!initial) return;
    const v = dhcpDraft.trim();
    if (!v) return;
    if (dhcpServers.includes(v)) {
      setDhcpDraft('');
      setDhcpAdding(false);
      return;
    }
    setDhcpPending('add');
    setDhcpError(null);
    try {
      await addSVIDhcpRelay(initial.device, {
        vlan_id: initial.vlanId,
        server: v,
      });
      setDhcpServers([...dhcpServers, v]);
      setDhcpDraft('');
      setDhcpAdding(false);
      onDone?.(`Added relay ${v} on VLAN ${initial.vlanId}.`, 'ok');
      invalidateSviQueries(queryClient);
    } catch (err) {
      setDhcpError(extractMessage(err, 'Add relay failed.'));
    } finally {
      setDhcpPending(null);
    }
  }

  async function removeDhcpServer(s: string) {
    if (!initial) return;
    setDhcpPending(`remove:${s}`);
    setDhcpError(null);
    try {
      await removeSVIDhcpRelay(initial.device, {
        vlan_id: initial.vlanId,
        server: s,
      });
      setDhcpServers(dhcpServers.filter((x) => x !== s));
      onDone?.(`Removed relay ${s} on VLAN ${initial.vlanId}.`, 'ok');
      invalidateSviQueries(queryClient);
    } catch (err) {
      setDhcpError(extractMessage(err, 'Remove relay failed.'));
    } finally {
      setDhcpPending(null);
    }
  }

  const canSubmit = ops.length > 0 && !mutation.isPending;

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
            {ops.length === 0
              ? 'No changes'
              : `${ops.length} pending change${ops.length === 1 ? '' : 's'}`}
          </span>
          <ModalSecondary onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </ModalSecondary>
          <ModalPrimary onClick={() => mutation.mutate()} disabled={!canSubmit}>
            {mutation.isPending
              ? progress
                ? `Saving ${progress.done}/${progress.total}…`
                : 'Saving…'
              : 'Save'}
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
            pending={dhcpPending}
            error={dhcpError}
            onAdd={addDhcpServer}
            onRemove={removeDhcpServer}
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
  pending,
  error,
  onAdd,
  onRemove,
}: {
  servers: string[];
  draft: string;
  onDraftChange: (v: string) => void;
  adding: boolean;
  onAddingChange: (v: boolean) => void;
  pending: string | null;
  error: string | null;
  onAdd: () => void;
  onRemove: (s: string) => void;
}) {
  const busy = pending !== null;
  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-md border border-panel-border p-3 flex flex-col gap-2">
        <p className="text-xs font-semibold uppercase tracking-wider text-muted mb-1">
          DHCP relay servers
        </p>

        {servers.map((s) => {
          const removing = pending === `remove:${s}`;
          return (
            <div key={s} className="flex items-center gap-2">
              <div className="flex-1 rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text font-mono">
                {s}
              </div>
              <button
                type="button"
                onClick={() => onRemove(s)}
                disabled={busy}
                aria-label={`Remove relay ${s}`}
                className="rounded-md border border-danger/50 text-danger px-3 py-2 text-sm leading-none hover:bg-danger/10 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                {removing ? '…' : '−'}
              </button>
            </div>
          );
        })}

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
              disabled={busy}
              className="flex-1 rounded-md bg-panel-elev border border-info px-3 py-2 text-sm text-text font-mono focus:outline-none focus:ring-2 focus:ring-info"
            />
            <button
              type="button"
              onClick={onAdd}
              disabled={busy || draft.trim() === ''}
              className="rounded-md border border-info text-info px-3 py-2 text-xs font-semibold uppercase tracking-wider hover:bg-info/10 disabled:opacity-40 disabled:cursor-not-allowed transition"
            >
              {pending === 'add' ? 'Adding…' : 'Add'}
            </button>
            <button
              type="button"
              onClick={() => {
                onDraftChange('');
                onAddingChange(false);
              }}
              disabled={busy}
              className="rounded-md border border-panel-border text-muted px-3 py-2 text-xs font-semibold uppercase tracking-wider hover:text-text disabled:opacity-40 transition"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => onAddingChange(true)}
            disabled={busy}
            className="w-full rounded-md border-2 border-dashed border-panel-border px-3 py-2 text-sm text-muted hover:text-text hover:border-muted disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            + Add server
          </button>
        )}
      </div>

      <p className="text-xs text-muted italic">
        Each row fires its own POST or DELETE — changes here don&apos;t wait
        for Save.
      </p>

      {error && (
        <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2">
          {error}
        </p>
      )}
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
