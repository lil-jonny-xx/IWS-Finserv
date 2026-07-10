'use client';
import { useEffect } from 'react';
import { navFor } from '@/app/lib/nav';
import { useDragScroll } from './DragScroll';

// The one section-tab strip, shared by every page (each page used to carry its
// own copy of this list — add new tabs HERE only). Rendered as a single
// horizontally scrollable row: touchpad/wheel scrolls natively, press-and-drag
// works via useDragScroll, the scrollbar is hidden (.nav-scroll) and the active
// tab is centred into view on load.
//
// Variants match the two existing looks:
//   pills — standalone strip on the asset pages (bordered pill per tab)
//   links — compact text tabs inside the dashboard / reports header bars
const TABS = [
  { href: '/dashboard',      label: 'Overview' },
  { href: '/mutual-funds',   label: 'Mutual Funds' },
  { href: '/equity',         label: 'Equity' },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/fno',            label: 'FnO' },
  { href: '/bank-accounts',  label: 'Banks' },
  { href: '/pms',            label: 'PMS' },
  { href: '/gold-silver',    label: 'Commodities' },
  { href: '/unlisted',       label: 'Unlisted' },
  { href: '/properties',     label: 'Properties' },
  { href: '/art',            label: 'Art' },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/manual-data',    label: 'Manual Data' },
  { href: '/reports',        label: 'Reports' },
  { href: '/assistant',      label: 'Assistant' },
  { href: '/account',        label: 'Account' },
];

const ITEM_CLASS = {
  pills: {
    active:   'bg-prime text-prime-fg',
    inactive: 'bg-card border border-rule text-dim hover:border-dim hover:text-ink',
  },
  links: {
    active:   'bg-prime/10 text-prime',
    inactive: 'text-dim hover:text-ink hover:bg-page',
  },
};

export default function NavTabs({
  active, role, variant = 'pills', className = '',
}: {
  active: string;
  role?: string | null;
  variant?: 'pills' | 'links';
  className?: string;
}) {
  const { ref, bind } = useDragScroll();

  // Bring the active tab into view (centred) without animating on first paint.
  useEffect(() => {
    const c = ref.current;
    const el = c?.querySelector<HTMLElement>('[aria-current="page"]');
    if (c && el) c.scrollLeft = el.offsetLeft - (c.clientWidth - el.offsetWidth) / 2;
  }, [ref]);

  const item = ITEM_CLASS[variant];
  return (
    <nav
      ref={ref}
      {...bind}
      aria-label="Sections"
      className={`nav-scroll flex overflow-x-auto min-w-0 max-w-full py-0.5 ${
        variant === 'pills' ? 'gap-1.5' : 'items-center gap-0.5'
      } ${className}`}
    >
      {navFor(TABS, role).map(({ href, label }) => (
        <a
          key={href}
          href={href}
          draggable={false}
          aria-current={href === active ? 'page' : undefined}
          className={`shrink-0 px-3 py-1.5 rounded text-xs font-medium transition-colors ${
            href === active ? item.active : item.inactive
          }`}
        >
          {label}
        </a>
      ))}
    </nav>
  );
}
