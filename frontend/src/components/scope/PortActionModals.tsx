'use client';

import { useState } from 'react';
import { batchUpdatePorts, resetPort } from '@/services/api';
import { runPortBatch, runPortBatchByDevice, type BatchResult } from './runPortBatch';
import { PortActionShell } from './PortActionShell';
import { FieldRow } from './VlanCreateModal';
import type { PortSelection } from './usePortSelection';

interface BaseProps {
  open: boolean;
  onClose: () => void;
  selection: PortSelection;
  onDone?: (result: BatchResult) => void;
}

// ── 1) Shutdown / Undo Shutdown / Reset (no input) ───────────────────────────

type ConfirmAction = 'shutdown' | 'undo-shutdown' | 'reset';

interface ConfirmProps extends BaseProps {
  action: ConfirmAction;
}

// `reset` (RF-PUERTO-10) has no batch equivalent -- it's exclusive with
// every other field server-side, doesn't fit in a `changes` list of
// specific fields (see runPortBatch.ts's docstring on runPortBatch()).
// shutdown/undo-shutdown are both just `admin_up`, batchable like any
// other field.
const CONFIRM_META: Record<
  ConfirmAction,
  {
    title: string;
    actionLabel: string;
    tone: 'default' | 'danger';
    warning: string;
  }
> = {
  shutdown: {
    title: 'Shutdown ports',
    actionLabel: 'Shutdown',
    tone: 'danger',
    warning:
      'Selected ports will be administratively down. Attached hosts lose link immediately.',
  },
  'undo-shutdown': {
    title: 'Undo shutdown',
    actionLabel: 'Enable',
    tone: 'default',
    warning: 'Selected ports will be administratively up again.',
  },
  reset: {
    title: 'Delete port config',
    actionLabel: 'Reset',
    tone: 'danger',
    warning:
      'This wipes VLAN / description / PoE / storm-control back to the vendor default. Not reversible.',
  },
};

export function PortConfirmModal({ open, onClose, selection, action, onDone }: ConfirmProps) {
  const meta = CONFIRM_META[action];
  return (
    <PortActionShell
      open={open}
      onClose={onClose}
      title={meta.title}
      selection={selection}
      actionLabel={meta.actionLabel}
      tone={meta.tone}
      onExecute={(progress) =>
        action === 'reset'
          ? runPortBatch(
              selection.refs,
              (ref) => resetPort(ref.device, { interface: ref.interface }),
              { onProgress: progress },
            )
          : runPortBatchByDevice(
              selection.byDevice,
              () => ({ admin_up: action === 'undo-shutdown' }),
              (device, changes) => batchUpdatePorts(device, { changes }),
              { onProgress: progress },
            )
      }
      onDone={onDone}
    >
      <p className="text-sm text-warning border border-warning/40 bg-warning/10 rounded px-3 py-2">
        {meta.warning}
      </p>
    </PortActionShell>
  );
}

// ── 2) Change description ────────────────────────────────────────────────────

export function PortDescriptionModal({ open, onClose, selection, onDone }: BaseProps) {
  const [description, setDescription] = useState('');
  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setDescription('');
        onClose();
      }}
      title="Change description"
      selection={selection}
      actionLabel="Apply"
      canExecute
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () => ({ description }),
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="New description">
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Empty string clears the description"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
        />
      </FieldRow>
    </PortActionShell>
  );
}

// ── 3) Set Port Mode (Access / Trunk) ────────────────────────────────────────

export function PortModeModal({ open, onClose, selection, onDone }: BaseProps) {
  const [mode, setMode] = useState<'access' | 'trunk'>('access');
  const [accessVlan, setAccessVlan] = useState('');
  const [nativeVlan, setNativeVlan] = useState('');
  const [allowedText, setAllowedText] = useState('');

  const accessParsed = Number(accessVlan);
  const nativeParsed = Number(nativeVlan);
  const allowedParsed = parseVlanList(allowedText);
  const canExecute =
    mode === 'access'
      ? isVlanId(accessParsed)
      : isVlanId(nativeParsed) && allowedParsed !== null;

  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setMode('access');
        setAccessVlan('');
        setNativeVlan('');
        setAllowedText('');
        onClose();
      }}
      title="Set port mode"
      selection={selection}
      actionLabel="Apply"
      canExecute={canExecute}
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () =>
            mode === 'access'
              ? { mode: 'access', access_vlan: accessParsed }
              : { mode: 'trunk', access_vlan: nativeParsed, allowed_vlans: allowedParsed ?? [] },
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="Port mode">
        <div className="flex items-center gap-4">
          <RadioLabel checked={mode === 'access'} onChange={() => setMode('access')}>
            Access
          </RadioLabel>
          <RadioLabel checked={mode === 'trunk'} onChange={() => setMode('trunk')}>
            Trunk
          </RadioLabel>
        </div>
      </FieldRow>

      {mode === 'access' ? (
        <FieldRow label="Access VLAN">
          <input
            type="number"
            min={1}
            max={4094}
            value={accessVlan}
            onChange={(e) => setAccessVlan(e.target.value)}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>
      ) : (
        <>
          <FieldRow label="Native VLAN (PVID)">
            <input
              type="number"
              min={1}
              max={4094}
              value={nativeVlan}
              onChange={(e) => setNativeVlan(e.target.value)}
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
          </FieldRow>
          <FieldRow label="Allowed VLANs">
            <input
              type="text"
              value={allowedText}
              onChange={(e) => setAllowedText(e.target.value)}
              placeholder="e.g. 10, 20, 100-105"
              className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
            />
            {allowedText !== '' && allowedParsed === null && (
              <p className="text-xs text-danger mt-1">
                Use comma-separated VLAN ids and optional ranges (e.g. `10, 20, 100-105`).
              </p>
            )}
          </FieldRow>
        </>
      )}
    </PortActionShell>
  );
}

// ── 4) Assign Native / Untagged VLAN ─────────────────────────────────────────

export function PortAccessVlanModal({ open, onClose, selection, onDone }: BaseProps) {
  const [vlan, setVlan] = useState('');
  const parsed = Number(vlan);
  const canExecute = isVlanId(parsed);
  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setVlan('');
        onClose();
      }}
      title="Assign Native VLAN (untagged)"
      selection={selection}
      actionLabel="Apply"
      canExecute={canExecute}
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () => ({ access_vlan: parsed }),
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="VLAN ID">
        <input
          type="number"
          min={1}
          max={4094}
          value={vlan}
          onChange={(e) => setVlan(e.target.value)}
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
        />
      </FieldRow>
    </PortActionShell>
  );
}

// ── 5) Assign Tagged VLANs ───────────────────────────────────────────────────

export function PortTrunkVlansModal({ open, onClose, selection, onDone }: BaseProps) {
  const [mode, setMode] = useState<'replace' | 'add' | 'remove'>('add');
  const [text, setText] = useState('');
  const parsed = parseVlanList(text);
  const canExecute = parsed !== null && parsed.length > 0;

  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setMode('add');
        setText('');
        onClose();
      }}
      title="Assign Tagged VLANs"
      selection={selection}
      actionLabel="Apply"
      canExecute={canExecute}
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () => ({ allowed_vlans: parsed ?? [], allowed_vlan_operation: mode }),
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="Operation">
        <div className="flex items-center gap-4">
          <RadioLabel checked={mode === 'add'} onChange={() => setMode('add')}>
            Add
          </RadioLabel>
          <RadioLabel checked={mode === 'remove'} onChange={() => setMode('remove')}>
            Remove
          </RadioLabel>
          <RadioLabel checked={mode === 'replace'} onChange={() => setMode('replace')}>
            Replace
          </RadioLabel>
        </div>
      </FieldRow>
      <FieldRow label="VLAN list">
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. 10, 20, 100-105"
          className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
        />
        {text !== '' && parsed === null && (
          <p className="text-xs text-danger mt-1">
            Use comma-separated VLAN ids and optional ranges (e.g. `10, 20, 100-105`).
          </p>
        )}
      </FieldRow>
    </PortActionShell>
  );
}

// ── 6) PoE Activate / Deactivate ─────────────────────────────────────────────

export function PortPoeModal({ open, onClose, selection, onDone }: BaseProps) {
  const [enabled, setEnabled] = useState(true);
  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setEnabled(true);
        onClose();
      }}
      title="PoE"
      selection={selection}
      actionLabel={enabled ? 'Activate' : 'Deactivate'}
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () => ({ poe_enabled: enabled }),
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="Action">
        <div className="flex items-center gap-4">
          <RadioLabel checked={enabled} onChange={() => setEnabled(true)}>
            Activate PoE
          </RadioLabel>
          <RadioLabel checked={!enabled} onChange={() => setEnabled(false)}>
            Deactivate PoE
          </RadioLabel>
        </div>
      </FieldRow>
    </PortActionShell>
  );
}

// ── 7) Storm Control ─────────────────────────────────────────────────────────

export function PortStormControlModal({ open, onClose, selection, onDone }: BaseProps) {
  const [enabled, setEnabled] = useState(true);
  const [threshold, setThreshold] = useState('10');
  const parsedThreshold = Number(threshold);
  const canExecute =
    !enabled ||
    (Number.isFinite(parsedThreshold) &&
      parsedThreshold > 0 &&
      parsedThreshold <= 100);
  return (
    <PortActionShell
      open={open}
      onClose={() => {
        setEnabled(true);
        setThreshold('10');
        onClose();
      }}
      title="Storm control"
      selection={selection}
      actionLabel="Apply"
      canExecute={canExecute}
      onExecute={(progress) =>
        runPortBatchByDevice(
          selection.byDevice,
          () => ({
            storm_control_enabled: enabled,
            storm_control_threshold: enabled ? parsedThreshold : null,
          }),
          (device, changes) => batchUpdatePorts(device, { changes }),
          { onProgress: progress },
        )
      }
      onDone={onDone}
    >
      <FieldRow label="Storm control">
        <div className="flex items-center gap-4">
          <RadioLabel checked={enabled} onChange={() => setEnabled(true)}>
            Enable
          </RadioLabel>
          <RadioLabel checked={!enabled} onChange={() => setEnabled(false)}>
            Disable
          </RadioLabel>
        </div>
      </FieldRow>
      {enabled && (
        <FieldRow label="Threshold %">
          <input
            type="number"
            min={1}
            max={100}
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            className="w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info"
          />
        </FieldRow>
      )}
    </PortActionShell>
  );
}

// ── Shared bits ──────────────────────────────────────────────────────────────

function RadioLabel({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: () => void;
  children: React.ReactNode;
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-text cursor-pointer">
      <input
        type="radio"
        checked={checked}
        onChange={onChange}
        className="h-4 w-4 accent-info"
      />
      {children}
    </label>
  );
}

function isVlanId(n: number): boolean {
  return Number.isInteger(n) && n >= 1 && n <= 4094;
}

/**
 * Parses `10, 20, 100-105` into `[10, 20, 100, 101, 102, 103, 104, 105]`.
 * Returns null when anything is invalid or the result contains out-of-range ids.
 */
function parseVlanList(text: string): number[] | null {
  const trimmed = text.trim();
  if (trimmed === '') return null;
  const out = new Set<number>();
  const chunks = trimmed.split(',').map((s) => s.trim()).filter(Boolean);
  for (const chunk of chunks) {
    const range = chunk.match(/^(\d+)\s*-\s*(\d+)$/);
    if (range) {
      const a = Number(range[1]);
      const b = Number(range[2]);
      if (!isVlanId(a) || !isVlanId(b) || a > b) return null;
      for (let v = a; v <= b; v += 1) out.add(v);
      continue;
    }
    const single = Number(chunk);
    if (!isVlanId(single)) return null;
    out.add(single);
  }
  if (out.size === 0) return null;
  return Array.from(out).sort((a, b) => a - b);
}
