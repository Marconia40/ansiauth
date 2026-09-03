import Link from 'next/link';

export interface Crumb {
  label: string;
  href?: string;
}

export function Breadcrumb({ items }: { items: Crumb[] }) {
  return (
    <nav aria-label="Breadcrumb" className="text-sm">
      <ol className="flex flex-wrap items-center gap-1 text-muted">
        {items.map((item, idx) => {
          const isLast = idx === items.length - 1;
          return (
            <li key={idx} className="flex items-center gap-1">
              {item.href && !isLast ? (
                <Link
                  href={item.href}
                  className="hover:text-text transition-colors uppercase tracking-wide"
                >
                  {item.label}
                </Link>
              ) : (
                <span
                  className={`uppercase tracking-wide ${
                    isLast ? 'text-text font-medium' : ''
                  }`}
                >
                  {item.label}
                </span>
              )}
              {!isLast && <span className="text-muted/60">&gt;</span>}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
