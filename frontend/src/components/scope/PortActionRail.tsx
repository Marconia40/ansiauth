'use client';

export type PortActionId =
  | 'mode'
  | 'access-vlan'
  | 'trunk-vlans'
  | 'shutdown'
  | 'undo-shutdown'
  | 'description'
  | 'storm-control'
  | 'poe'
  | 'reset';

interface RailItem {
  id: PortActionId;
  label: string;
  tone?: 'default' | 'danger';
}

const RAIL: RailItem[] = [
  { id: 'mode', label: 'Select Type-Port · Access / Trunk' },
  { id: 'access-vlan', label: 'Assign Native VLAN (UT)' },
  { id: 'trunk-vlans', label: 'Assign Tagged VLANs (T)' },
  { id: 'shutdown', label: 'Shutdown Ports', tone: 'danger' },
  { id: 'undo-shutdown', label: 'Undo Shutdown' },
  { id: 'description', label: 'Change Description' },
  { id: 'storm-control', label: 'Set Storm-Control' },
  { id: 'poe', label: 'Activate / Deactivate PoE' },
  { id: 'reset', label: 'Delete Ports Config', tone: 'danger' },
];

interface Props {
  disabled: boolean;
  onOpen: (id: PortActionId) => void;
}

/** Right-hand rail with the 9 fixed bulk-port operations. */
export function PortActionRail({ disabled, onOpen }: Props) {
  return (
    <div className="flex flex-col gap-2 sticky top-2">
      {RAIL.map((item) => {
        const cls =
          item.tone === 'danger'
            ? 'border-danger/60 text-danger hover:bg-danger/10'
            : 'border-panel-border text-text hover:bg-panel-elev';
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => onOpen(item.id)}
            disabled={disabled}
            className={`rounded-md border ${cls} text-xs font-semibold uppercase tracking-wide px-3 py-2 text-center leading-tight disabled:opacity-40 disabled:cursor-not-allowed transition-colors`}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
