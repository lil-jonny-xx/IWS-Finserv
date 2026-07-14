'use client';
import { useEffect } from 'react';
import { navFor } from '@/app/lib/nav';
import { DYNAMIC_CATEGORY_LABELS } from '@/app/lib/manualCategories';
import { useNavCoverage } from '@/app/lib/navCoverage';
import { useDragScroll } from './DragScroll';

// The one section-tab strip, shared by every page (each page used to carry its
// own copy of this list — add new tabs HERE only). Rendered as a single
// horizontally scrollable row: touchpad/wheel scrolls natively, press-and-drag
// works via useDragScroll, the scrollbar is hidden (.nav-scroll) and the active
// tab is centred into view on load.
//
// The list is data-driven via /api/v1/nav-coverage:
//   - asset tabs in DATA_GATED hide while no entity has data in that section
//     (the tab for the page being viewed always stays);
//   - Manual Data categories without a dedicated page (AIF, PPF, …) get a tab
//     automatically once their first entry exists anywhere — see
//     DYNAMIC_CATEGORY_LABELS. They link to the generic /assets/<category>
//     page and slot in after the asset tabs, before Realised Gains.
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
  { href: '/trades',         label: 'Trades' },
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

type Tab = { href: string; label: string };

// Section tabs hidden while empty; the rest (Overview, Realised Gains, Manual
// Data, Reports, Assistant, Account) are utility pages that always show.
const DATA_GATED = new Set([
  '/mutual-funds', '/equity', '/foreign-equity', '/fno', '/bank-accounts',
  '/pms', '/gold-silver', '/unlisted', '/properties', '/art',
]);

export default function NavTabs({
  active, role, variant = 'pills', className = '',
}: {
  active: string;
  role?: string | null;
  variant?: 'pills' | 'links';
  className?: string;
}) {
  const { ref, bind } = useDragScroll();
  const cov = useNavCoverage();

  const dynTabs: Tab[] = cov
    ? Object.keys(DYNAMIC_CATEGORY_LABELS)
        .filter(c => (cov.categories[c]?.length ?? 0) > 0)
        .map(c => ({ href: `/assets/${c}`, label: DYNAMIC_CATEGORY_LABELS[c] }))
    : [];

  // Bring the active tab into view (centred) without animating on first paint.
  useEffect(() => {
    const c = ref.current;
    const el = c?.querySelector<HTMLElement>('[aria-current="page"]');
    if (c && el) c.scrollLeft = el.offsetLeft - (c.clientWidth - el.offsetWidth) / 2;
  }, [ref, dynTabs]);

  // Plain mouse wheel scrolls the strip horizontally (touchpads already send
  // deltaX natively). Non-passive listener so we can stop the page from
  // scrolling vertically underneath — but only while the strip overflows.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.deltaY || Math.abs(e.deltaX) >= Math.abs(e.deltaY)) return;
      if (el.scrollWidth <= el.clientWidth) return;
      el.scrollLeft += e.deltaY;
      e.preventDefault();
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [ref]);

  const tabs: Tab[] = TABS.filter(t =>
    t.href === active || !DATA_GATED.has(t.href) || !cov || (cov.sections[t.href]?.length ?? 0) > 0);
  if (dynTabs.length) {
    const at = tabs.findIndex(t => t.href === '/realised-gains');
    tabs.splice(at === -1 ? tabs.length : at, 0, ...dynTabs);
  }

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
      {navFor(tabs, role).map(({ href, label }) => (
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
