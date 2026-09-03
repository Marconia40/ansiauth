import type { ReactNode } from 'react';
import { Breadcrumb, type Crumb } from './Breadcrumb';
import { ScopeTabs, type ScopeTab } from './ScopeTabs';

interface Props {
  crumbs: Crumb[];
  tabsBase: string;
  tabs?: ScopeTab[];
  /** Right-aligned actions in the header (e.g. RefreshButton). */
  actions?: ReactNode;
  children: ReactNode;
}

/**
 * Common wrapper for every scope-level page (Org/Site/Group/Device).
 * Renders the breadcrumb, the tabs strip and reserves the body area.
 */
export function ScopeShell({ crumbs, tabsBase, tabs, actions, children }: Props) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-4">
        <Breadcrumb items={crumbs} />
        {actions && <div className="shrink-0">{actions}</div>}
      </div>
      <ScopeTabs base={tabsBase} tabs={tabs} />
      <div className="pt-2">{children}</div>
    </div>
  );
}
