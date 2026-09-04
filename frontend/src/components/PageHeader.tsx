import type { ReactNode } from 'react';

interface Props {
  title: string;
  actions?: ReactNode;
}

export function PageHeader({ title, actions }: Props) {
  return (
    <div className="flex items-center justify-between mb-6">
      <h1 className="text-2xl font-bold text-text">{title}</h1>
      {actions && <div className="flex gap-2">{actions}</div>}
    </div>
  );
}
