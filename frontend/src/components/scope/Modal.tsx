'use client';

import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Optional footer slot — usually action buttons on the right. */
  footer?: ReactNode;
  children: ReactNode;
  /** Tailwind width class for the panel. Defaults to a medium modal. */
  widthClass?: string;
}

/**
 * Bare-bones modal with an overlay, Escape-to-close, click-outside-to-close
 * and a portal that mounts on <body>. Deliberately headless — no header icon,
 * no scroll lock library; the body is a plain flex column so callers control
 * spacing.
 */
export function Modal({
  open,
  onClose,
  title,
  footer,
  children,
  widthClass = 'w-full max-w-lg',
}: Props) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', handleKey);
    // Lock body scroll while the modal is open so background pages don't jump.
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', handleKey);
      document.body.style.overflow = originalOverflow;
    };
  }, [open, onClose]);

  if (!open || typeof document === 'undefined') return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`${widthClass} max-h-[90vh] flex flex-col rounded-lg bg-panel border border-panel-border shadow-2xl`}
      >
        <header className="px-5 py-3 border-b border-panel-border flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-muted hover:text-text text-xl leading-none px-2"
          >
            ×
          </button>
        </header>
        <div className="flex-1 overflow-y-auto p-5">{children}</div>
        {footer && (
          <footer className="px-5 py-3 border-t border-panel-border flex items-center justify-end gap-2">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  );
}
