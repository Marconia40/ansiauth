import type { AssignmentRole, RoleAssignment, User } from '@/types/user';

const ROLE_STYLE: Record<AssignmentRole, string> = {
  observer: 'bg-panel-elev text-muted border border-panel-border',
  operator: 'bg-amber-100 text-amber-700',
  admin: 'bg-blue-100 text-blue-700',
};

// Highest-privilege chip: kept distinct from any per-scope role so it never
// visually blends with a regular ``admin`` grant.
const SYSTEM_WIDE_STYLE = 'bg-red-100 text-red-700 font-semibold';

const MAX_INLINE_BADGES = 3;

function scopeLabel(g: RoleAssignment): string {
  const site = g.site_name ?? `Site #${g.site_id}`;
  if (g.device_group_id != null) {
    const group = g.device_group_name ?? `Group #${g.device_group_id}`;
    return `${site}/${group}`;
  }
  return site;
}

function GrantChip({ grant }: { grant: RoleAssignment }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${ROLE_STYLE[grant.role]}`}
      title={`${scopeLabel(grant)} — ${grant.role}`}
    >
      {scopeLabel(grant)}: {grant.role}
    </span>
  );
}

interface AccessBadgesProps {
  user: User;
  grants: RoleAssignment[] | undefined;
  isLoading?: boolean;
  onSeeMore?: () => void;
}

/**
 * Compact access summary for the users list — replaces the legacy
 * synthesized-role column. Renders one of:
 *
 *   * ``SYSTEM-WIDE ADMIN`` badge when ``user.is_system_admin`` is true.
 *   * Up to three per-scope chips (``Site: role`` or ``Site/Group: role``)
 *     followed by ``+N more`` when the user holds more grants.
 *   * ``No access`` when the user has zero grants and is not system-admin.
 */
export function AccessBadges({ user, grants, isLoading, onSeeMore }: AccessBadgesProps) {
  if (user.is_system_admin) {
    return (
      <span
        className={`inline-flex items-center px-2 py-0.5 rounded text-xs ${SYSTEM_WIDE_STYLE}`}
        title="Bypasses every per-site and per-group permission"
      >
        SYSTEM-WIDE ADMIN
      </span>
    );
  }
  if (isLoading || grants === undefined) {
    return <span className="text-xs text-muted/70 italic">Loading…</span>;
  }
  if (grants.length === 0) {
    return <span className="text-xs text-muted/70 italic">No access</span>;
  }
  const visible = grants.slice(0, MAX_INLINE_BADGES);
  const overflow = grants.length - visible.length;
  return (
    <div className="flex flex-wrap gap-1 items-center">
      {visible.map((g) => (
        <GrantChip key={g.id} grant={g} />
      ))}
      {overflow > 0 && (
        onSeeMore ? (
          <button
            type="button"
            onClick={onSeeMore}
            className="text-xs text-info hover:underline"
          >
            +{overflow} more
          </button>
        ) : (
          <span className="text-xs text-muted">+{overflow} more</span>
        )
      )}
    </div>
  );
}
