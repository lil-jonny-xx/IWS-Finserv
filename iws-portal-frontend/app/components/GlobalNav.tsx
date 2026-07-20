'use client';
// The one section-nav bar, mounted once in the root layout.
//
// This strip used to be rendered by every page individually — 18 call sites, each
// passing its own `active` and its own copy of the user's role, in two different
// looks (bordered pills on the asset pages, compact text links inside the header
// bars of Overview / Reports / Realised Gains / Manual Data). That meant the nav
// appeared in a different place and style depending on where you were, and adding
// a tab meant trusting 18 pages to agree.
//
// Now it is global and identical everywhere: the Overview's look, hoisted. `active`
// comes from the route rather than a prop (NavTabs matches href by exact path, so
// dynamic routes like /assets/aif resolve on their own), and the role comes from
// the shared session fetch.
//
// Hidden on the unauthenticated routes, matching TopBar — there is nothing to
// navigate to before sign-in.
import Image from 'next/image';
import { usePathname } from 'next/navigation';
import NavTabs from './NavTabs';
import { useMe } from '@/app/lib/useMe';

const HIDDEN_ROUTES = new Set(['/', '/forgot-password']);

export default function GlobalNav() {
  const pathname = usePathname();
  const hidden = HIDDEN_ROUTES.has(pathname);
  const me = useMe(!hidden);

  if (hidden) return null;

  return (
    <header
      style={{ background: 'var(--card)', borderBottom: '1px solid var(--rule)' }}
      className="px-4 sm:px-6 py-3"
    >
      <div className="max-w-screen-xl mx-auto flex items-center gap-3 min-w-0">
        <a href="/dashboard" className="flex items-center gap-3 shrink-0" aria-label="Overview">
          <Image src="/logo.png" alt="" width={342} height={346} priority className="h-8 w-auto" />
          <span className="text-sm font-semibold text-ink hidden sm:block">Rajani MIS</span>
        </a>
        {/* Visible at every width — this is now the only section nav, where the
            Overview's copy used to be sm-and-up only and the asset pages carried
            their own mobile-visible strip. The tabs scroll horizontally. */}
        <NavTabs active={pathname} role={me?.role} variant="links" className="ml-1 sm:ml-4" />
      </div>
    </header>
  );
}
