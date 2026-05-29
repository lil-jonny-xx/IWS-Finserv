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

const ASSET_CLASS_COLORS: Record<string, string> = {
  EQUITY:       'var(--chart-equity)',
  FIXED_INCOME: 'var(--chart-fixed)',
  ALTERNATES:   'var(--chart-alt)',
};

interface DonutSegment {
  cls:   string;
  label: string;
  value: number;
  pct:   number;
}

function DonutChart({ segments }: { segments: DonutSegment[] }) {
  const r     = 36;
  const sw    = 13;
  const circ  = 2 * Math.PI * r;
  const size  = 100;
  const c     = size / 2;

  const valid = segments.filter(s => s.pct > 0.005);
  let cumLen  = 0;
  const arcs  = valid.map(seg => {
    const len        = seg.pct * circ;
    const dashoffset = circ - cumLen;
    cumLen += len;
    return { ...seg, len, dashoffset };
  });

  return (
    <div className="flex items-center gap-4 shrink-0">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        width="72"
        height="72"
        aria-hidden="true"
        style={{ transform: 'rotate(-90deg)' }}
      >
        <circle
          cx={c} cy={c} r={r}
          fill="none"
          stroke="var(--rule)"
          strokeWidth={sw}
        />
        {arcs.map(arc => (
          <circle
            key={arc.cls}
            cx={c} cy={c} r={r}
            fill="none"
            strokeWidth={sw}
            strokeLinecap="butt"
            strokeDasharray={`${arc.len} ${circ - arc.len}`}
            strokeDashoffset={arc.dashoffset}
            style={{ stroke: ASSET_CLASS_COLORS[arc.cls] ?? 'var(--muted)' }}
          />
        ))}
      </svg>
      <div className="space-y-1.5">
        {valid.map(seg => (
          <div key={seg.cls} className="flex items-center gap-2">
            <span
              className="w-2 h-2 rounded-full shrink-0"
              style={{ background: ASSET_CLASS_COLORS[seg.cls] ?? 'var(--muted)' }}
            />
            <span className="text-xs text-ghost whitespace-nowrap">{seg.label}</span>
            <span className="text-xs font-medium text-dim tabular-nums ml-1">
              {Math.round(seg.pct * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

type SortKey = 'scheme' | 'units' | 'invested' | 'nav' | 'currentValue';
type SortDir = 'asc' | 'desc';

function sortHoldings(holdings: Holding[], key: SortKey, dir: SortDir): Holding[] {
  return [...holdings].sort((a, b) => {
    let va: number | string, vb: number | string;
    switch (key) {
      case 'scheme':
        va = a.security_name.toLowerCase(); vb = b.security_name.toLowerCase(); break;
      case 'units':
        va = a.quantity; vb = b.quantity; break;
      case 'invested':
        va = a.invested_amount; vb = b.invested_amount; break;
      case 'nav':
        va = a.nav ?? -Infinity; vb = b.nav ?? -Infinity; break;
      case 'currentValue':
      default:
        va = a.current_value ?? (a.nav != null ? a.quantity * a.nav : -Infinity);
        vb = b.current_value ?? (b.nav != null ? b.quantity * b.nav : -Infinity);
    }
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', {
    day: 'numeric', month: 'short', year: 'numeric',
  });
}

function HoldingsSection({ data }: { data: HoldingsData }) {
  const [sortKey, setSortKey] = useState<SortKey>('currentValue');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  if (data.holdings.length === 0) return <HoldingsEmpty />;

  function handleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('desc');
    }
  }

  function SortHint({ col }: { col: SortKey }) {
    const active = sortKey === col;
    return (
      <span aria-hidden className={`ml-1 text-[9px] ${active ? 'text-prime' : 'opacity-30'}`}>
        {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
      </span>
    );
  }

  const grouped: Record<string, Holding[]> = Object.fromEntries(
    ASSET_CLASS_ORDER.map(cls => [
      cls,
      data.holdings.filter(h => h.asset_class === cls),
    ])
  );

  const activeClasses = ASSET_CLASS_ORDER.filter(
    cls => (grouped[cls]?.length ?? 0) > 0
  );

  const totalCurrentValue = data.holdings.reduce((sum, h) => {
    const cv = h.current_value ?? (h.nav != null ? Math.round(h.quantity * h.nav) : null);
    return cv != null ? sum + cv : sum;
  }, 0);
  const allHaveNav = data.holdings.every(h => h.current_value != null || h.nav != null);
  const gain    = allHaveNav && totalCurrentValue > 0 ? totalCurrentValue - data.total_invested : null;
  const gainPct = gain != null && data.total_invested > 0 ? (gain / data.total_invested) * 100 : null;

  const allocationSegments: DonutSegment[] = activeClasses.map(cls => {
    const subtotal = grouped[cls]!.reduce((s, h) => s + h.invested_amount, 0);
    return {
      cls,
      label: ASSET_CLASS_LABELS[cls] ?? cls,
      value: subtotal,
      pct:   data.total_invested > 0 ? subtotal / data.total_invested : 0,
    };
  });

  const lastUpdated = data.holdings
    .map(h => h.last_updated)
    .filter((d): d is string => d != null)
    .sort()
    .at(-1) ?? null;

  return (
    <section aria-label="MF Holdings">
      <div className="bg-card rounded-lg border border-rule overflow-hidden">

        <div className="px-5 sm:px-6 pt-5 pb-5 border-b border-rule">
          <h2 className="text-base font-semibold text-ink mb-4">
            MF Holdings
            {lastUpdated && (
              <span className="ml-3 text-xs font-normal text-ghost">
                as of {formatDate(lastUpdated)}
              </span>
            )}
          </h2>
          <div className="flex flex-col sm:flex-row sm:items-start gap-5">
            <div className="flex flex-wrap gap-x-8 gap-y-3 flex-1">
              <div>
                <p className="text-xs text-ghost mb-0.5">Total Invested</p>
                <p className="text-sm font-semibold text-ink tabular-nums">
                  {formatINR(data.total_invested)}
                </p>
              </div>
              {totalCurrentValue > 0 && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Current Value</p>
                  <p className="text-sm font-semibold text-ink tabular-nums">
                    {formatINR(totalCurrentValue)}
                  </p>
                </div>
              )}
              {gain != null && gainPct != null && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Unrealized P&amp;L</p>
                  <p
                    className="text-sm font-semibold tabular-nums"
                    style={{ color: gain >= 0 ? 'var(--gain)' : 'var(--peril)' }}
                  >
                    {gain >= 0 ? '+' : '−'}{formatINR(Math.abs(gain))}{' '}
                    <span className="text-xs font-medium opacity-75">
                      ({gain >= 0 ? '+' : '−'}{Math.abs(gainPct).toFixed(1)}%)
                    </span>
                  </p>
                </div>
              )}
              <div>
                <p className="text-xs text-ghost mb-0.5">Holdings</p>
                <p className="text-sm font-semibold text-ink tabular-nums">
                  {data.total_holdings}
                </p>
              </div>
            </div>
            {allocationSegments.length > 1 && (
              <DonutChart segments={allocationSegments} />
            )}
          </div>
        </div>

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
                  aria-sort={sortKey === 'scheme' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button
                    onClick={() => handleSort('scheme')}
                    className="inline-flex items-center hover:text-ink transition-colors"
                  >
                    Scheme<SortHint col="scheme" />
                  </button>
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
                  aria-sort={sortKey === 'units' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button
                    onClick={() => handleSort('units')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors"
                  >
                    Units<SortHint col="units" />
                  </button>
                </th>
                <th
                  scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'invested' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button
                    onClick={() => handleSort('invested')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors"
                  >
                    Invested<SortHint col="invested" />
                  </button>
                </th>
                <th
                  scope="col"
                  className="text-right px-4 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'nav' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button
                    onClick={() => handleSort('nav')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors"
                  >
                    NAV<SortHint col="nav" />
                  </button>
                </th>
                <th
                  scope="col"
                  className="text-right px-5 sm:px-6 py-3 text-xs font-medium text-ghost border-b border-rule whitespace-nowrap"
                  aria-sort={sortKey === 'currentValue' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button
                    onClick={() => handleSort('currentValue')}
                    className="inline-flex items-center ml-auto hover:text-ink transition-colors"
                  >
                    Current Value<SortHint col="currentValue" />
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              {ASSET_CLASS_ORDER.map(cls => {
                const group = grouped[cls];
                if (!group || group.length === 0) return null;

                const subtotal = group.reduce((s, h) => s + h.invested_amount, 0);
                const sorted   = sortHoldings(group, sortKey, sortDir);

                return (
                  <Fragment key={cls}>
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
                    {sorted.map(h => {
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
          <div className="space-y-2">
            <div className="h-8 sm:h-9 bg-rule rounded-md w-52 animate-pulse" />
            <div className="h-3 bg-rule rounded w-44 animate-pulse" />
          </div>
          <div className="h-9 bg-rule rounded-md w-24 animate-pulse" />
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
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl sm:text-3xl font-bold text-ink wrap-break-word">
              Welcome, {user.full_name}
            </h1>
            <p className="text-sm text-ghost mt-0.5">{user.email} · {formatRole(user.role)}</p>
          </div>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            aria-busy={loggingOut}
            className="border border-wire text-dim px-4 py-2 rounded-md text-sm hover:border-dim hover:text-ink disabled:opacity-50 disabled:cursor-not-allowed transition-colors duration-150 shrink-0"
          >
            {loggingOut ? 'Signing out...' : 'Sign out'}
          </button>
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
