'use client';
import { useEffect, useState, useCallback, Fragment } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_TIMEOUT = 30 * 60 * 1000;

interface User {
  full_name: string;
  email: string;
  role: string;
  entity_id: string | number;
}

interface Holding {
  id: number;
  isin: string;
  security_name: string;
  security_type: string;
  asset_class: string;
  amfi_code: string;
  folio_number: string;
  quantity: number;
  avg_cost: number | null;
  cost_basis: number | null;
  invested_amount: number;
  nav: number | null;
  current_value: number | null;
  first_invested_date: string | null;
  last_updated: string | null;
}

interface HoldingsData {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  total_invested: number;
  holdings: Holding[];
}

const ASSET_CLASS_ORDER = ['EQUITY', 'FIXED_INCOME', 'ALTERNATES'] as const;

const ASSET_CLASS_LABELS: Record<string, string> = {
  EQUITY:       'Equity',
  FIXED_INCOME: 'Fixed Income',
  ALTERNATES:   'Alternates',
};

const SEC_TYPE_LABELS: Record<string, string> = {
  MF_EQUITY:    'Equity Fund',
  MF_DEBT:      'Debt Fund',
  MF_HYBRID:    'Hybrid Fund',
  MF_LIQUID:    'Liquid Fund',
  MF_FOF:       'Fund of Funds',
  MF_ELSS:      'ELSS',
  MF_ETF:       'Index ETF',
  MF_ARBITRAGE: 'Arbitrage Fund',
  GOLD_ETF:     'Gold ETF',
  MF_OTHER:     'Other MF',
};

function formatINR(n: number): string {
  const abs = Math.round(Math.abs(n));
  const str = abs.toString();
  const lastThree = str.slice(-3);
  const rest = str.slice(0, -3);
  const grouped =
    rest.length > 0
      ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + lastThree
      : lastThree;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

function formatRole(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1).toLowerCase();
}

function HoldingsSkeleton() {
  return (
    <section aria-label="MF Holdings" aria-busy="true">
      <span role="status" className="sr-only">Loading holdings</span>
      <div className="bg-card rounded-lg border border-rule overflow-hidden" aria-hidden="true">
        <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
          <div className="h-3.5 bg-rule rounded w-24 mb-4 animate-pulse" />
          <div className="flex flex-wrap gap-8">
            <div className="space-y-1.5">
              <div className="h-2.5 bg-rule rounded w-24 animate-pulse" />
              <div className="h-4 bg-rule rounded w-28 animate-pulse" />
            </div>
            <div className="space-y-1.5">
              <div className="h-2.5 bg-rule rounded w-14 animate-pulse" />
              <div className="h-4 bg-rule rounded w-8 animate-pulse" />
            </div>
            <div className="space-y-1.5">
              <div className="h-2.5 bg-rule rounded w-20 animate-pulse" />
              <div className="h-4 bg-rule rounded w-6 animate-pulse" />
            </div>
          </div>
        </div>
        <div>
          {[...Array(8)].map((_, i) => (
            <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
              <div className="flex-1 space-y-1.5">
                <div className="h-3 bg-rule rounded max-w-xs animate-pulse" />
                <div className="h-2 bg-rule rounded w-20 animate-pulse" />
              </div>
              <div className="h-3 bg-rule rounded w-20 animate-pulse self-center" />
              <div className="h-3 bg-rule rounded w-16 animate-pulse self-center" />
              <div className="h-3 bg-rule rounded w-20 animate-pulse self-center" />
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function HoldingsError({ message }: { message: string }) {
  return (
    <section aria-label="Holdings">
      <div
        role="alert"
        className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4"
      >
        <p className="text-sm font-medium text-dim">Could not load holdings</p>
        <p className="text-xs text-ghost mt-1">{message}</p>
      </div>
    </section>
  );
}

function HoldingsEmpty() {
  return (
    <section aria-label="MF Holdings">
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No holdings on record</p>
        <p className="text-xs text-ghost mx-auto" style={{ maxWidth: '40ch' }}>
          Holdings will appear once your CAS statement has been imported.
        </p>
      </div>
    </section>
  );
}

function HoldingsSection({ data }: { data: HoldingsData }) {
  if (data.holdings.length === 0) return <HoldingsEmpty />;

  const grouped: Record<string, Holding[]> = Object.fromEntries(
    ASSET_CLASS_ORDER.map(cls => [
      cls,
      data.holdings.filter(h => h.asset_class === cls),
    ])
  );

  const activeClassCount = ASSET_CLASS_ORDER.filter(
    cls => (grouped[cls]?.length ?? 0) > 0
  ).length;

  return (
    <section aria-label="MF Holdings">
      <div className="bg-card rounded-lg border border-rule overflow-hidden">

        {/* Card header: title + summary stats */}
        <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
          <h2 className="text-base font-semibold text-dim mb-4">MF Holdings</h2>
          <div className="flex flex-wrap gap-x-8 gap-y-3">
            <div>
              <p className="text-xs text-ghost mb-0.5">Total Invested</p>
              <p className="text-sm font-semibold text-ink tabular-nums">
                {formatINR(data.total_invested)}
              </p>
            </div>
            <div>
              <p className="text-xs text-ghost mb-0.5">Holdings</p>
              <p className="text-sm font-semibold text-ink tabular-nums">
                {data.total_holdings}
              </p>
            </div>
            <div>
              <p className="text-xs text-ghost mb-0.5">Asset Classes</p>
              <p className="text-sm font-semibold text-ink tabular-nums">
                {activeClassCount}
              </p>
            </div>
          </div>
        </div>

        {/* Table — horizontally scrollable on mobile */}
        <div
          className="overflow-x-auto"
          role="region"
          aria-label="Holdings table"
          tabIndex={0}
        >
          <table className="w-full text-sm" style={{ minWidth: '700px' }}>
            <thead>
              <tr>
                <th
                  scope="col"
                  className="text-left px-5 sm:px-6 py-3 text-xs font-medium text-ghost border-b border-rule"
                >
                  Scheme
                </th>
                <th
                  scope="col"
                  className="text-left px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                >
                  Folio
                </th>
                <th
                  scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                >
                  Units
                </th>
                <th
                  scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                >
                  Invested
                </th>
                <th
                  scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                >
                  NAV
                </th>
                <th
                  scope="col"
                  className="text-right px-5 sm:px-6 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                >
                  Current Value
                </th>
              </tr>
            </thead>
            <tbody>
              {ASSET_CLASS_ORDER.map(cls => {
                const group = grouped[cls];
                if (!group || group.length === 0) return null;

                const subtotal = group.reduce((s, h) => s + h.invested_amount, 0);

                return (
                  <Fragment key={cls}>
                    {/* Asset class group header */}
                    <tr>
                      <td colSpan={6} className="px-5 sm:px-6 py-2 bg-page">
                        <span className="text-xs font-semibold text-dim">
                          {ASSET_CLASS_LABELS[cls] ?? cls}
                        </span>
                        <span className="ml-2 text-xs text-ghost">
                          {formatINR(subtotal)}
                        </span>
                      </td>
                    </tr>

                    {/* Holding rows */}
                    {group.map(h => {
                      const currentVal =
                        h.current_value ??
                        (h.nav != null ? Math.round(h.quantity * h.nav) : null);

                      return (
                        <tr
                          key={h.id}
                          className="border-t border-rule hover:bg-page transition-colors duration-100"
                        >
                          <td className="px-5 sm:px-6 py-3.5">
                            <p className="text-ink leading-snug">{h.security_name}</p>
                            <p className="text-xs text-ghost mt-0.5">
                              {SEC_TYPE_LABELS[h.security_type] ?? h.security_type}
                            </p>
                          </td>
                          <td className="px-4 py-3.5 font-mono text-xs text-dim whitespace-nowrap align-top">
                            {h.folio_number}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {h.quantity.toFixed(3)}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {formatINR(h.invested_amount)}
                          </td>
                          <td className="px-4 py-3.5 text-right tabular-nums text-dim whitespace-nowrap align-top">
                            {h.nav != null ? '₹' + h.nav.toFixed(2) : '—'}
                          </td>
                          <td className="px-5 sm:px-6 py-3.5 text-right tabular-nums text-ink whitespace-nowrap align-top">
                            {currentVal != null ? formatINR(currentVal) : '—'}
                          </td>
                        </tr>
                      );
                    })}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

export default function DashboardPage() {
  const [user, setUser]                   = useState<User | null>(null);
  const [error, setError]                 = useState<string | null>(null);
  const [loggingOut, setLoggingOut]       = useState(false);
  const [holdings, setHoldings]           = useState<HoldingsData | null>(null);
  const [holdingsLoading, setHoldingsLoading] = useState(true);
  const [holdingsError, setHoldingsError] = useState<string | null>(null);
  const router = useRouter();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
      if (!res.ok) {
        setError('Sign out failed. Please try again.');
        setLoggingOut(false);
        return;
      }
    } catch {
      setError('Network error during sign out. Please try again.');
      setLoggingOut(false);
      return;
    }
    router.push('/');
  }, [router]);

  // Idle timeout — unchanged
  useEffect(() => {
    let idleTimer: NodeJS.Timeout;
    const resetTimer = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(handleLogout, IDLE_TIMEOUT);
    };
    const events = ['mousedown', 'keypress', 'touchstart'] as const;
    events.forEach(e => window.addEventListener(e, resetTimer));
    window.addEventListener('scroll', resetTimer, { passive: true });
    resetTimer();
    return () => {
      clearTimeout(idleTimer);
      events.forEach(e => window.removeEventListener(e, resetTimer));
      window.removeEventListener('scroll', resetTimer);
    };
  }, [handleLogout]);

  // Fetch session user
  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/v1/me`, {
      credentials: 'include',
      signal: controller.signal,
    })
      .then(res => {
        if (res.status === 401) { router.push('/'); return null; }
        if (!res.ok) throw new Error('Unable to load your session. Please try again.');
        return res.json();
      })
      .then(data => { if (data) setUser(data); })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setError(err.message);
        setTimeout(() => router.push('/'), 3000);
      });
    return () => controller.abort();
  }, [router]);

  // Fetch holdings in parallel with the user fetch
  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/v1/holdings`, {
      credentials: 'include',
      signal: controller.signal,
    })
      .then(res => {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error('Unable to load holdings.');
        return res.json();
      })
      .then(data => {
        if (data) setHoldings(data);
        setHoldingsLoading(false);
      })
      .catch(err => {
        if (err.name === 'AbortError') return;
        setHoldingsError(err.message);
        setHoldingsLoading(false);
      });
    return () => controller.abort();
  }, []);

  // Full-page error (session fetch failed)
  if (error) return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-page px-4 py-10">
      <div className="bg-card p-6 sm:p-8 rounded-lg shadow-sm border border-rule text-center w-full max-w-sm" role="alert">
        <p className="text-peril font-medium mb-2">Session error</p>
        <p className="text-dim text-sm">{error}</p>
        <p className="text-ghost text-xs mt-2">
          Redirecting to login, or{' '}
          <a href="/" className="text-prime underline-offset-2 hover:underline">go now</a>.
        </p>
      </div>
    </main>
  );

  // Full-page skeleton (user not yet resolved)
  if (!user) return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8" aria-busy="true">
      <span role="status" aria-live="polite" className="sr-only">Loading dashboard</span>
      <div className="max-w-4xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6" aria-hidden="true">
          <div className="h-8 sm:h-9 bg-rule rounded-md w-52 animate-pulse" />
          <div className="h-9 bg-rule rounded-md w-24 animate-pulse" />
        </div>
        <div className="bg-card p-5 sm:p-6 rounded-lg border border-rule mb-4 animate-pulse" aria-hidden="true">
          <div className="h-3.5 bg-rule rounded w-20 mb-4" />
          <div className="space-y-3">
            <div className="h-3 bg-rule rounded w-64" />
            <div className="h-3 bg-rule rounded w-24" />
          </div>
        </div>
        <div className="bg-card rounded-lg border border-rule overflow-hidden animate-pulse" aria-hidden="true">
          <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
            <div className="h-3.5 bg-rule rounded w-24 mb-4" />
            <div className="flex gap-8">
              <div className="space-y-1.5">
                <div className="h-2.5 bg-rule rounded w-24" />
                <div className="h-4 bg-rule rounded w-28" />
              </div>
              <div className="space-y-1.5">
                <div className="h-2.5 bg-rule rounded w-14" />
                <div className="h-4 bg-rule rounded w-8" />
              </div>
              <div className="space-y-1.5">
                <div className="h-2.5 bg-rule rounded w-20" />
                <div className="h-4 bg-rule rounded w-6" />
              </div>
            </div>
          </div>
          {[...Array(5)].map((_, i) => (
            <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
              <div className="flex-1 space-y-1.5">
                <div className="h-3 bg-rule rounded max-w-xs" />
                <div className="h-2 bg-rule rounded w-20" />
              </div>
              <div className="h-3 bg-rule rounded w-20 self-center" />
              <div className="h-3 bg-rule rounded w-16 self-center" />
              <div className="h-3 bg-rule rounded w-20 self-center" />
            </div>
          ))}
        </div>
      </div>
    </main>
  );

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-4xl mx-auto">

        {/* Header */}
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <h1 className="text-2xl sm:text-3xl font-bold text-ink break-words min-w-0 flex-1">
            Welcome, {user.full_name}
          </h1>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            aria-busy={loggingOut}
            className="border border-wire text-dim px-4 py-2 rounded-md text-sm hover:border-dim hover:text-ink disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 shrink-0"
          >
            {loggingOut ? 'Signing out...' : 'Sign out'}
          </button>
        </div>

        {/* Profile card — unchanged */}
        <div className="bg-card p-5 sm:p-6 rounded-lg shadow-sm border border-rule mb-4">
          <h2 className="text-base font-semibold text-dim mb-3">Your profile</h2>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
            <dt className="font-medium text-dim">Email</dt>
            <dd className="text-ink break-all">{user.email}</dd>
            <dt className="font-medium text-dim">Role</dt>
            <dd className="text-ink">{formatRole(user.role)}</dd>
          </dl>
        </div>

        {/* Holdings — skeleton while loading, inline error on failure */}
        <div className="mb-4">
          {holdingsLoading && !holdingsError && <HoldingsSkeleton />}
          {holdingsError  && <HoldingsError message={holdingsError} />}
          {!holdingsLoading && !holdingsError && holdings && (
            <HoldingsSection data={holdings} />
          )}
        </div>

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Session expires after 30 minutes of inactivity
        </p>
      </div>
    </main>
  );
}
