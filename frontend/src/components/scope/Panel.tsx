import type { ReactNode } from 'react';

interface Props {
  title?: string;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

export function Panel({ title, actions, className, children }: Props) {
  return (
    <section
      className={`bg-panel border border-panel-border rounded-lg p-4 shadow-sm ${
        className ?? ''
      }`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between mb-3 pb-2 border-b border-panel-border">
          {title && <h2 className="text-base font-semibold text-text">{title}</h2>}
          {actions && <div>{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function PlaceholderPanel({ label }: { label: string }) {
  return (
    <div className="bg-panel border border-dashed border-panel-border rounded-lg p-8 text-center text-muted italic">
      {label}
    </div>
  );
}
