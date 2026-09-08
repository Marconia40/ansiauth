'use client';

import { useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { batchUpdatePorts } from '@/services/api';
import { useJobNotifications } from '@/context/JobNotificationContext';
import type { PortBatchChangeItem, TrunkVlanMode } from '@/types/port';
import { Modal } from './Modal';
import {
  FieldRow,
  ModalPrimary,
  ModalSecondary,
} from './VlanCreateModal';
import type { PortSelection } from './usePortSelection';
import { runPortBatchByDevice, type BatchResult } from './runPortBatch';

type Tab = 'general' | 'mode' | 'trunk' | 'storm' | 'poe';

interface Props {
  open: boolean;
  onClose: () => void;
  selection: PortSelection;
  onDone?: (result: BatchResult) => void;
}

/** Unified editor for the N selected ports. Each tab owns one field group;
 * each group has an explicit "Modify" toggle because with multiple ports we
 * can't diff against a single "initial" (N ports may have N distinct current
 * values). Save collects every enabled group into one `PortBatchChangeItem`
 * per interface and sends 1 `POST .../ports/batch` per device — the whole
 * bundle is 1 SSH connection per device instead of 1 per field. `group_job_id`
 * for each device call is registered with the global JobNotifications system
 * so the toast is clickable → JobDetailModal like Routes / SVI. */
export function PortEditModal({ open, onClose, selection, onDone }: Props) {
  const queryClient = useQueryClient();
  const { trackGroupJob } = useJobNotifications();

  const [tab, setTab] = useState<Tab>('general');

  // ── General ────────────────────────────────────────────────────────────────
  const [descriptionEnabled, setDescriptionEnabled] = useState(false);
  const [description, setDescription] = useState('');
  const [adminEnabled, setAdminEnabled] = useState(false);
  const [adminUp, setAdminUp] = useState(true);

  // ── Mode (full mode change) ────────────────────────────────────────────────
  const [modeEnabled, setModeEnabled] = useState(false);
  const [modeKind, setModeKind] = useState<'access' | 'trunk'>('access');
  const [modeAccessVlan, setModeAccessVlan] = useState('');
  const [modeNativeVlan, setModeNativeVlan] = useState('');
  const [modeAllowedText, setModeAllowedText] = useState('');

  // ── Trunk VLANs (incremental add/remove/replace, no mode change) ──────────
  const [trunkEnabled, setTrunkEnabled] = useState(false);
  const [trunkOp, setTrunkOp] = useState<TrunkVlanMode>('add');
  const [trunkText, setTrunkText] = useState('');

  // ── Storm control ──────────────────────────────────────────────────────────
  const [stormEnabledToggle, setStormEnabledToggle] = useState(false);
  const [stormOn, setStormOn] = useState(true);
  const [stormThreshold, setStormThreshold] = useState('10');

  // ── PoE ────────────────────────────────────────────────────────────────────
  const [poeEnabledToggle, setPoeEnabledToggle] = useState(false);
  const [poeOn, setPoeOn] = useState(true);

  // ── Run state ──────────────────────────────────────────────────────────────
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [result, setResult] = useState<BatchResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reset form each time the modal opens.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    setTab('general');
    setDescriptionEnabled(false);
    setDescription('');
    setAdminEnabled(false);
    setAdminUp(true);
    setModeEnabled(false);
    setModeKind('access');
    setModeAccessVlan('');
    setModeNativeVlan('');
    setModeAllowedText('');
    setTrunkEnabled(false);
    setTrunkOp('add');
    setTrunkText('');
    setStormEnabledToggle(false);
    setStormOn(true);
    setStormThreshold('10');
    setPoeEnabledToggle(false);
    setPoeOn(true);
    setRunning(false);
    setProgress(null);
    setResult(null);
    setError(null);
  }, [open]);
  /* eslint-enable react-hooks/set-state-in-effect */

  // ── Validation per tab ─────────────────────────────────────────────────────
  const modeAllowedParsed = parseVlanList(modeAllowedText);
  const trunkParsed = parseVlanList(trunkText);

  const modeAccessValid = isVlanId(Number(modeAccessVlan));
  const modeTrunkValid =
    isVlanId(Number(modeNativeVlan)) && modeAllowedParsed !== null && modeAllowedParsed.length > 0;
  const stormThresholdParsed = Number(stormThreshold);
  const stormValid =
    !stormOn ||
    (Number.isFinite(stormThresholdParsed) && stormThresholdParsed > 0 && stormThresholdParsed <= 100);
  const trunkValid = trunkParsed !== null && trunkParsed.length > 0;

  const tabValidity = {
    general: true, // description empty = clear; admin has a default. Nothing to validate.
    mode: !modeEnabled || (modeKind === 'access' ? modeAccessValid : modeTrunkValid),
    trunk: !trunkEnabled || trunkValid,
    storm: !stormEnabledToggle || stormValid,
    poe: true,
  } as const;

  const allValid = Object.values(tabValidity).every(Boolean);

  // ── Dirty tabs (visual indicator on the tab bar) ───────────────────────────
  const dirtyByTab: Record<Tab, boolean> = {
    general: descriptionEnabled || adminEnabled,
    mode: modeEnabled,
    trunk: trunkEnabled,
    storm: stormEnabledToggle,
    poe: poeEnabledToggle,
  };

  const totalChanges = Object.values(dirtyByTab).filter(Boolean).length;

  // If mode is set to trunk AND the trunk-vlans tab is also enabled, the
  // former's allowed_vlans+replace overrides the latter — flag it so the
  // user knows one tab is being ignored.
  const trunkOverriddenByMode = modeEnabled && modeKind === 'trunk' && trunkEnabled;

  // ── Build the change item shared by every selected port ────────────────────
  const changeTemplate = useMemo<Omit<PortBatchChangeItem, 'interface'>>(() => {
    const change: Omit<PortBatchChangeItem, 'interface'> = {};

    if (descriptionEnabled) change.description = description;
    if (adminEnabled) change.admin_up = adminUp;

    if (modeEnabled) {
      if (modeKind === 'access') {
        change.mode = 'access';
        change.access_vlan = Number(modeAccessVlan);
      } else {
        // trunk — access_vlan carries the native VLAN / PVID here.
        change.mode = 'trunk';
        change.access_vlan = Number(modeNativeVlan);
        change.allowed_vlans = modeAllowedParsed ?? [];
        change.allowed_vlan_operation = 'replace';
      }
    }

    // Incremental trunk-vlans only if the mode tab isn't already replacing them.
    if (trunkEnabled && !(modeEnabled && modeKind === 'trunk')) {
      change.allowed_vlans = trunkParsed ?? [];
      change.allowed_vlan_operation = trunkOp;
    }

    if (stormEnabledToggle) {
      change.storm_control_enabled = stormOn;
      change.storm_control_threshold = stormOn ? stormThresholdParsed : null;
    }

    if (poeEnabledToggle) change.poe_enabled = poeOn;

    return change;
  }, [
    descriptionEnabled,
    description,
    adminEnabled,
    adminUp,
    modeEnabled,
    modeKind,
    modeAccessVlan,
    modeNativeVlan,
    modeAllowedParsed,
    trunkEnabled,
    trunkOp,
    trunkParsed,
    stormEnabledToggle,
    stormOn,
    stormThresholdParsed,
    poeEnabledToggle,
    poeOn,
  ]);

  const canSubmit =
    totalChanges > 0 && allValid && !running && selection.count > 0;

  async function handleSave() {
    setRunning(true);
    setError(null);
    setResult(null);
    setProgress({ done: 0, total: selection.count });
    try {
      const res = await runPortBatchByDevice(
        selection.byDevice,
        () => changeTemplate,
        (device, changes) => batchUpdatePorts(device, { changes }),
        { onProgress: (done, total) => setProgress({ done, total }) },
      );
      setResult(res);

      // Register every successful device batch as a group job so the global
      // JobNotifications toast is clickable → JobDetailModal (like Routes/SVI).
      for (const { device, groupJobId } of res.groupJobsByDevice ?? []) {
        const count = selection.byDevice.get(device)?.length ?? 0;
        trackGroupJob(
          groupJobId,
          `Update ${count} port${count === 1 ? '' : 's'} on ${device}`,
        );
      }

      // Refresh port envelopes for every touched device.
      const devices = new Set(selection.refs.map((r) => r.device));
      for (const d of devices) {
        queryClient.invalidateQueries({ queryKey: ['ports', 'synced', d] });
      }

      onDone?.(res);
    } catch (err) {
      setError(extractMessage(err, 'Save failed.'));
    } finally {
      setRunning(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={() => (running ? undefined : onClose())}
      title={`Edit ${selection.count} port${selection.count === 1 ? '' : 's'}`}
      widthClass="w-full max-w-2xl"
      footer={
        result ? (
          <ModalSecondary onClick={onClose}>Close</ModalSecondary>
        ) : (
          <>
            <span className="text-xs text-muted mr-auto">
              {totalChanges === 0
                ? 'No changes'
                : `${totalChanges} pending change${totalChanges === 1 ? '' : 's'}`}
            </span>
            <ModalSecondary onClick={onClose} disabled={running}>
              Cancel
            </ModalSecondary>
            <ModalPrimary onClick={handleSave} disabled={!canSubmit}>
              {running
                ? progress
                  ? `Saving ${progress.done}/${progress.total}…`
                  : 'Saving…'
                : 'Save'}
            </ModalPrimary>
          </>
        )
      }
    >
      <div className="flex flex-col gap-4">
        <SelectionBanner selection={selection} />

        <TabBar tab={tab} onChange={setTab} dirty={dirtyByTab} disabled={running} />

        {tab === 'general' && (
          <GeneralTab
            descriptionEnabled={descriptionEnabled}
            onDescriptionEnabledChange={setDescriptionEnabled}
            description={description}
            onDescriptionChange={setDescription}
            adminEnabled={adminEnabled}
            onAdminEnabledChange={setAdminEnabled}
            adminUp={adminUp}
            onAdminUpChange={setAdminUp}
          />
        )}
        {tab === 'mode' && (
          <ModeTab
            enabled={modeEnabled}
            onEnabledChange={setModeEnabled}
            kind={modeKind}
            onKindChange={setModeKind}
            accessVlan={modeAccessVlan}
            onAccessVlanChange={setModeAccessVlan}
            nativeVlan={modeNativeVlan}
            onNativeVlanChange={setModeNativeVlan}
            allowedText={modeAllowedText}
            onAllowedTextChange={setModeAllowedText}
            allowedParsed={modeAllowedParsed}
          />
        )}
        {tab === 'trunk' && (
          <TrunkVlansTab
            enabled={trunkEnabled}
            onEnabledChange={setTrunkEnabled}
            op={trunkOp}
            onOpChange={setTrunkOp}
            text={trunkText}
            onTextChange={setTrunkText}
            parsed={trunkParsed}
            overridden={trunkOverriddenByMode}
          />
        )}
        {tab === 'storm' && (
          <StormControlTab
            enabled={stormEnabledToggle}
            onEnabledChange={setStormEnabledToggle}
            on={stormOn}
            onOnChange={setStormOn}
            threshold={stormThreshold}
            onThresholdChange={setStormThreshold}
          />
        )}
        {tab === 'poe' && (
          <PoeTab
            enabled={poeEnabledToggle}
            onEnabledChange={setPoeEnabledToggle}
            on={poeOn}
            onOnChange={setPoeOn}
          />
        )}

        {trunkOverriddenByMode && (
          <p className="text-xs text-warning border border-warning/40 bg-warning/10 rounded px-3 py-2">
            The Mode tab is set to Trunk and already replaces the allowed VLAN
            list — the Trunk VLANs tab will be ignored for this Save.
          </p>
        )}

        {error && (
          <p className="text-sm text-danger border border-danger/40 bg-danger/10 rounded px-3 py-2 whitespace-pre-wrap">
            {error}
          </p>
        )}

        {result && <ResultView result={result} />}
      </div>
    </Modal>
  );
}

// ── Tabs bar ─────────────────────────────────────────────────────────────────

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
    { key: 'mode', label: 'Mode' },
    { key: 'trunk', label: 'Trunk VLANs' },
    { key: 'storm', label: 'Storm Control' },
    { key: 'poe', label: 'PoE' },
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
                title="Modification queued"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

// ── Tabs bodies ──────────────────────────────────────────────────────────────

function GeneralTab({
  descriptionEnabled,
  onDescriptionEnabledChange,
  description,
  onDescriptionChange,
  adminEnabled,
  onAdminEnabledChange,
  adminUp,
  onAdminUpChange,
}: {
  descriptionEnabled: boolean;
  onDescriptionEnabledChange: (v: boolean) => void;
  description: string;
  onDescriptionChange: (v: string) => void;
  adminEnabled: boolean;
  onAdminEnabledChange: (v: boolean) => void;
  adminUp: boolean;
  onAdminUpChange: (v: boolean) => void;
}) {
  return (
    <div className="flex flex-col gap-5">
      <Section
        title="Description"
        enabled={descriptionEnabled}
        onEnabledChange={onDescriptionEnabledChange}
      >
        <FieldRow label="New description">
          <input
            type="text"
            value={description}
            onChange={(e) => onDescriptionChange(e.target.value)}
            disabled={!descriptionEnabled}
            placeholder="Leave empty to clear the description"
            className={inputCls(descriptionEnabled)}
          />
        </FieldRow>
      </Section>

      <Section
        title="Admin state"
        enabled={adminEnabled}
        onEnabledChange={onAdminEnabledChange}
      >
        <div className="flex items-center gap-2">
          <SegButton
            active={adminUp === true}
            onClick={() => onAdminUpChange(true)}
            tone="ok"
            disabled={!adminEnabled}
          >
            Up
          </SegButton>
          <SegButton
            active={adminUp === false}
            onClick={() => onAdminUpChange(false)}
            tone="danger"
            disabled={!adminEnabled}
          >
            Down / Shutdown
          </SegButton>
        </div>
      </Section>
    </div>
  );
}

function ModeTab({
  enabled,
  onEnabledChange,
  kind,
  onKindChange,
  accessVlan,
  onAccessVlanChange,
  nativeVlan,
  onNativeVlanChange,
  allowedText,
  onAllowedTextChange,
  allowedParsed,
}: {
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  kind: 'access' | 'trunk';
  onKindChange: (v: 'access' | 'trunk') => void;
  accessVlan: string;
  onAccessVlanChange: (v: string) => void;
  nativeVlan: string;
  onNativeVlanChange: (v: string) => void;
  allowedText: string;
  onAllowedTextChange: (v: string) => void;
  allowedParsed: number[] | null;
}) {
  return (
    <Section title="Port mode" enabled={enabled} onEnabledChange={onEnabledChange}>
      <div className="flex flex-col gap-4">
        <FieldRow label="Mode">
          <div className="flex items-center gap-4">
            <RadioLabel
              checked={kind === 'access'}
              onChange={() => onKindChange('access')}
              disabled={!enabled}
            >
              Access
            </RadioLabel>
            <RadioLabel
              checked={kind === 'trunk'}
              onChange={() => onKindChange('trunk')}
              disabled={!enabled}
            >
              Trunk
            </RadioLabel>
          </div>
        </FieldRow>

        {kind === 'access' ? (
          <FieldRow label="Access VLAN">
            <input
              type="number"
              min={1}
              max={4094}
              value={accessVlan}
              onChange={(e) => onAccessVlanChange(e.target.value)}
              disabled={!enabled}
              className={inputCls(enabled)}
            />
          </FieldRow>
        ) : (
          <>
            <FieldRow label="Native VLAN (untagged / PVID)">
              <input
                type="number"
                min={1}
                max={4094}
                value={nativeVlan}
                onChange={(e) => onNativeVlanChange(e.target.value)}
                disabled={!enabled}
                className={inputCls(enabled)}
              />
            </FieldRow>
            <FieldRow label="Allowed / Tagged VLANs">
              <input
                type="text"
                value={allowedText}
                onChange={(e) => onAllowedTextChange(e.target.value)}
                disabled={!enabled}
                placeholder="e.g. 10, 20, 100-105"
                className={inputCls(enabled)}
              />
              {enabled && allowedText !== '' && allowedParsed === null && (
                <p className="text-xs text-danger mt-1">
                  Use comma-separated VLAN ids and optional ranges (e.g. `10, 20, 100-105`).
                </p>
              )}
            </FieldRow>
          </>
        )}
      </div>
    </Section>
  );
}

function TrunkVlansTab({
  enabled,
  onEnabledChange,
  op,
  onOpChange,
  text,
  onTextChange,
  parsed,
  overridden,
}: {
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  op: TrunkVlanMode;
  onOpChange: (v: TrunkVlanMode) => void;
  text: string;
  onTextChange: (v: string) => void;
  parsed: number[] | null;
  overridden: boolean;
}) {
  return (
    <Section
      title="Trunk VLANs (incremental)"
      enabled={enabled}
      onEnabledChange={onEnabledChange}
      description="Add, remove, or replace the allowed VLANs on ports that are already trunk. Use the Mode tab for a full access ↔ trunk change."
    >
      <div className="flex flex-col gap-4">
        <FieldRow label="Operation">
          <div className="flex items-center gap-4">
            <RadioLabel
              checked={op === 'add'}
              onChange={() => onOpChange('add')}
              disabled={!enabled}
            >
              Add
            </RadioLabel>
            <RadioLabel
              checked={op === 'remove'}
              onChange={() => onOpChange('remove')}
              disabled={!enabled}
            >
              Remove
            </RadioLabel>
            <RadioLabel
              checked={op === 'replace'}
              onChange={() => onOpChange('replace')}
              disabled={!enabled}
            >
              Replace
            </RadioLabel>
          </div>
        </FieldRow>
        <FieldRow label="VLAN list">
          <input
            type="text"
            value={text}
            onChange={(e) => onTextChange(e.target.value)}
            disabled={!enabled}
            placeholder="e.g. 10, 20, 100-105"
            className={inputCls(enabled)}
          />
          {enabled && text !== '' && parsed === null && (
            <p className="text-xs text-danger mt-1">
              Use comma-separated VLAN ids and optional ranges (e.g. `10, 20, 100-105`).
            </p>
          )}
        </FieldRow>
        {overridden && (
          <p className="text-xs text-warning">
            The Mode tab is currently set to Trunk and replaces the allowed VLAN
            list — this section will be ignored on Save.
          </p>
        )}
      </div>
    </Section>
  );
}

function StormControlTab({
  enabled,
  onEnabledChange,
  on,
  onOnChange,
  threshold,
  onThresholdChange,
}: {
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  on: boolean;
  onOnChange: (v: boolean) => void;
  threshold: string;
  onThresholdChange: (v: string) => void;
}) {
  return (
    <Section title="Storm control" enabled={enabled} onEnabledChange={onEnabledChange}>
      <div className="flex flex-col gap-4">
        <FieldRow label="State">
          <div className="flex items-center gap-4">
            <RadioLabel
              checked={on}
              onChange={() => onOnChange(true)}
              disabled={!enabled}
            >
              Enable
            </RadioLabel>
            <RadioLabel
              checked={!on}
              onChange={() => onOnChange(false)}
              disabled={!enabled}
            >
              Disable
            </RadioLabel>
          </div>
        </FieldRow>
        {on && (
          <FieldRow label="Threshold %">
            <input
              type="number"
              min={1}
              max={100}
              value={threshold}
              onChange={(e) => onThresholdChange(e.target.value)}
              disabled={!enabled}
              className={inputCls(enabled)}
            />
          </FieldRow>
        )}
      </div>
    </Section>
  );
}

function PoeTab({
  enabled,
  onEnabledChange,
  on,
  onOnChange,
}: {
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  on: boolean;
  onOnChange: (v: boolean) => void;
}) {
  return (
    <Section title="PoE" enabled={enabled} onEnabledChange={onEnabledChange}>
      <FieldRow label="Action">
        <div className="flex items-center gap-4">
          <RadioLabel
            checked={on}
            onChange={() => onOnChange(true)}
            disabled={!enabled}
          >
            Activate PoE
          </RadioLabel>
          <RadioLabel
            checked={!on}
            onChange={() => onOnChange(false)}
            disabled={!enabled}
          >
            Deactivate PoE
          </RadioLabel>
        </div>
      </FieldRow>
    </Section>
  );
}

// ── Small shared UI ──────────────────────────────────────────────────────────

function Section({
  title,
  enabled,
  onEnabledChange,
  description,
  children,
}: {
  title: string;
  enabled: boolean;
  onEnabledChange: (v: boolean) => void;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-md border border-panel-border p-4 flex flex-col gap-3">
      <label className="flex items-center gap-2 cursor-pointer select-none">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => onEnabledChange(e.target.checked)}
          className="h-4 w-4 accent-info"
        />
        <span className="text-sm font-semibold text-text">Modify {title}</span>
      </label>
      {description && (
        <p className="text-xs text-muted -mt-1">{description}</p>
      )}
      <div className={enabled ? '' : 'opacity-50'}>{children}</div>
    </div>
  );
}

function SegButton({
  active,
  onClick,
  tone,
  disabled,
  children,
}: {
  active: boolean;
  onClick: () => void;
  tone: 'ok' | 'danger';
  disabled?: boolean;
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
      disabled={disabled}
      className={`rounded-md border px-3 py-1 text-xs font-semibold uppercase tracking-wider transition disabled:opacity-40 disabled:cursor-not-allowed ${
        active ? activeCls : idleCls
      }`}
    >
      {children}
    </button>
  );
}

function RadioLabel({
  checked,
  onChange,
  disabled,
  children,
}: {
  checked: boolean;
  onChange: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label
      className={`flex items-center gap-2 text-sm text-text ${
        disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'
      }`}
    >
      <input
        type="radio"
        checked={checked}
        onChange={onChange}
        disabled={disabled}
        className="h-4 w-4 accent-info"
      />
      {children}
    </label>
  );
}

function SelectionBanner({ selection }: { selection: PortSelection }) {
  return (
    <div className="rounded-md border border-panel-border bg-panel-elev/60 px-3 py-2 text-sm">
      Operating on{' '}
      <span className="font-semibold text-text tabular-nums">{selection.count}</span>{' '}
      port{selection.count === 1 ? '' : 's'} across{' '}
      <span className="font-semibold text-text tabular-nums">{selection.deviceCount}</span>{' '}
      device{selection.deviceCount === 1 ? '' : 's'}.
    </div>
  );
}

function ResultView({ result }: { result: BatchResult }) {
  const total = result.success + result.failed;
  const allOk = result.failed === 0;
  return (
    <div
      className={`rounded-md border px-3 py-2 text-sm ${
        allOk
          ? 'border-success/60 bg-success/10 text-success'
          : 'border-warning/60 bg-warning/10 text-warning'
      }`}
    >
      <p className="font-semibold">
        {allOk ? 'All ports succeeded' : 'Finished with errors'}:{' '}
        <span className="tabular-nums">
          {result.success}/{total}
        </span>{' '}
        OK, <span className="tabular-nums">{result.failed}</span> failed.
      </p>
      {result.errors.length > 0 && (
        <ul className="mt-2 max-h-32 overflow-y-auto space-y-1 text-xs text-text">
          {result.errors.map(({ ref, error }) => (
            <li key={`${ref.device}::${ref.interface}`}>
              <span className="font-mono">
                {ref.device} · {ref.interface}
              </span>{' '}
              — {error}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function inputCls(enabled: boolean): string {
  return `w-full rounded-md bg-panel-elev border border-panel-border px-3 py-2 text-sm text-text focus:outline-none focus:ring-2 focus:ring-info ${
    enabled ? '' : 'opacity-60 cursor-not-allowed'
  }`;
}

function isVlanId(n: number): boolean {
  return Number.isInteger(n) && n >= 1 && n <= 4094;
}

/** Parses `10, 20, 100-105` into `[10, 20, 100, 101, 102, 103, 104, 105]`.
 * Returns null when anything is invalid or the result contains out-of-range ids. */
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

function extractMessage(err: unknown, fallback: string): string {
  const e = err as {
    response?: { data?: { detail?: string; message?: string } };
    message?: string;
  } | null;
  return (
    e?.response?.data?.detail ??
    e?.response?.data?.message ??
    e?.message ??
    fallback
  );
}
