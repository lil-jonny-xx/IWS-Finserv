'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { navFor } from '@/app/lib/nav';
import EquityTable, { type EquityHoldingRow, type EquityTotals } from './components/EquityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface EquityResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
}

interface ActivityTrade {
  entity_name: string;
  security_name: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  amount: number;
  realized_pnl: number | null;
}
interface ActivityResponse {
  date: string;
  buy_count: number;
  sell_count: number;
  realized_pnl_total: number;
  trades: ActivityTrade[];
}

// Buys/sells detected today by the intraday snapshot worker (source='snapshot').
// Prices are the snapshot LTP at detection, not the exact fill — see the API docstring.
function TradedToday({ data, showEntityCol }: { data: ActivityResponse; showEntityCol: boolean }) {
  const [open, setOpen] = useState(true);
  if (!data.trades.length) return null;
  const inr = (v: number) => v.toLocaleString('en-IN', { maximumFractionDigits: 2 });
  const signed = (v: number) => (v < 0 ? '−₹' : '₹') + inr(Math.abs(v));

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden mb-5">
      <button
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-5 py-3 text-left hover:bg-page transition-colors"
      >
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-ink">Traded today</span>
          <span className="text-xs text-ghost">
            {data.buy_count} buy{data.buy_count === 1 ? '' : 's'} · {data.sell_count} sell{data.sell_count === 1 ? '' : 's'}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {data.sell_count > 0 && (
            <span className="text-xs text-ghost">
              Realised P&amp;L{' '}
              <span className="font-semibold" style={{ color: data.realized_pnl_total >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {signed(data.realized_pnl_total)}
              </span>
            </span>
          )}
          <span className="text-xs text-dim">{open ? '▲' : '▼'}</span>
        </div>
      </button>
      {open && (
        <div className="overflow-x-auto border-t border-rule">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-page text-dim">
                {showEntityCol && <th className="px-3 py-2 text-left font-semibold">Entity</th>}
                <th className="px-3 py-2 text-left font-semibold">Security</th>
                <th className="px-3 py-2 text-left font-semibold">Side</th>
                <th className="px-3 py-2 text-right font-semibold">Qty</th>
                <th className="px-3 py-2 text-right font-semibold">Rate</th>
                <th className="px-3 py-2 text-right font-semibold">Value</th>
                <th className="px-3 py-2 text-right font-semibold">Realised P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t, i) => {
                const sell = t.side === 'SELL';
                return (
                  <tr key={i} className="border-t border-rule">
                    {showEntityCol && <td className="px-3 py-2 text-dim">{t.entity_name}</td>}
                    <td className="px-3 py-2 text-ink">{t.security_name}</td>
                    <td className="px-3 py-2">
                      <span
                        className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                        style={{ color: sell ? 'var(--peril)' : 'var(--gain)',
                                 border: `1px solid ${sell ? 'var(--peril)' : 'var(--gain)'}` }}
                      >
                        {t.side}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right text-dim">{t.quantity.toLocaleString('en-IN')}</td>
                    <td className="px-3 py-2 text-right text-dim">₹{inr(t.price)}</td>
                    <td className="px-3 py-2 text-right text-dim">₹{inr(t.amount)}</td>
                    <td
                      className="px-3 py-2 text-right font-medium"
                      style={{ color: t.realized_pnl == null ? 'var(--ghost)'
                                     : t.realized_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}
                    >
                      {t.realized_pnl == null ? '—' : signed(t.realized_pnl)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function EntitySwitcher({
  entities, selectedId, onSelect,
}: {
  entities: Entity[]; selectedId: number | null; onSelect: (id: number | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null, name: 'All' }, ...entities.map(e => ({ id: e.id, name: e.name }))].map(tab => {
        const active = tab.id === selectedId;
        return (
          <button
            key={tab.id ?? 'all'}
            role="tab"
            aria-selected={active}
            onClick={() => onSelect(tab.id ?? null)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active
                ? 'bg-prime text-prime-fg'
                : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
            }`}
          >
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden" aria-hidden="true">
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="h-3.5 bg-rule rounded w-32 mb-4 animate-pulse" />
        <div className="flex flex-wrap gap-8">
          {[24, 20, 20].map((w, i) => (
            <div key={i} className="space-y-1.5">
              <div className={`h-2.5 bg-rule rounded w-${w} animate-pulse`} />
              <div className="h-4 bg-rule rounded w-24 animate-pulse" />
            </div>
          ))}
        </div>
      </div>
      {[...Array(6)].map((_, i) => (
        <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
          <div className="h-3 bg-rule rounded w-16 animate-pulse" />
          <div className="h-3 bg-rule rounded w-20 animate-pulse" />
          <div className="flex-1 h-3 bg-rule rounded animate-pulse" />
        </div>
      ))}
    </div>
  );
}

export default function EquityPage() {
  const router = useRouter();
  const [user, setUser]                   = useState<User | null>(null);
  const [entities, setEntities]           = useState<Entity[]>([]);
  const [selectedId, setSelectedId]       = useState<number | null>(null);
  const [data, setData]                   = useState<EquityResponse | null>(null);
  const [activity, setActivity]           = useState<ActivityResponse | null>(null);
  const [loading, setLoading]             = useState(true);
  const [error, setError]                 = useState<string | null>(null);
  const [retryCount, setRetryCount]       = useState(0);
  const [lastUpdated, setLastUpdated]     = useState<Date | null>(null);
  const [liveActive, setLiveActive]       = useState(false);
  const intervalRef                       = useRef<ReturnType<typeof setInterval> | null>(null);
  const didInitialLoad                    = useRef(false);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u) {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : [])
            .then((ents: Entity[]) => setEntities(ents))
            .catch(() => {});
        }
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    const controller = new AbortController();
    // Show the skeleton only on the very first load. Background refreshes (auto-refresh
    // every minute) update values in place without unmounting the table, so the user's
    // sort / filters / search are preserved.
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/equity/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load equity holdings.'); return r.json(); })
      .then((d: EquityResponse | null) => {
        if (d) { setData(d); setLastUpdated(new Date()); }
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  // Today's detected buys/sells (snapshot worker). Same scope + refresh cadence as
  // holdings; failures are silent so they never disturb the main table.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/equity/activity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ActivityResponse | null) => { if (d) setActivity(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selectedId, retryCount]);

  // Auto-refresh every 60 s during market hours (09:15–15:30 IST Mon–Fri)
  useEffect(() => {
    function isMarketOpen() {
      const now = new Date();
      const ist = new Date(now.toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
      const day = ist.getDay();
      if (day === 0 || day === 6) return false;
      const mins = ist.getHours() * 60 + ist.getMinutes();
      return mins >= 9 * 60 + 15 && mins < 15 * 60 + 30;
    }

    if (intervalRef.current) clearInterval(intervalRef.current);
    const active = isMarketOpen();
    setLiveActive(active);
    if (!active) return;

    intervalRef.current = setInterval(() => {
      if (!isMarketOpen()) { clearInterval(intervalRef.current!); setLiveActive(false); return; }
      setRetryCount(c => c + 1);
    }, 60_000);

    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [selectedId]);

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Equity Portfolio</h1>
            <div className="flex items-center gap-2 mt-0.5">
              {liveActive ? (
                <span className="flex items-center gap-1.5 text-xs text-gain font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-gain animate-pulse inline-block" />
                  Live · updates every minute
                </span>
              ) : (
                <span className="text-sm text-ghost">Direct stock holdings across all brokers</span>
              )}
              {lastUpdated && (
                <span className="text-xs text-ghost">
                  · updated {lastUpdated.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                </span>
              )}
            </div>
          </div>
          <nav className="flex gap-1.5" aria-label="Sections">
            {navFor([
              { href: '/dashboard', label: 'Overview' },
              { href: '/mutual-funds', label: 'Mutual Funds' },
              { href: '/equity', label: 'Equity', active: true },
              { href: '/foreign-equity', label: 'Foreign Equity' },
              { href: '/bank-accounts', label: 'Banks' },
              { href: '/pms', label: 'PMS' },
              { href: '/gold-silver', label: 'Commodities' },
              { href: '/unlisted', label: 'Unlisted' },
              { href: '/properties', label: 'Properties' },
              { href: '/art', label: 'Art' },
              { href: '/realised-gains', label: 'Realised Gains' },
              { href: '/manual-data', label: 'Manual Data' },
              { href: '/reports', label: 'Reports' },
              { href: '/assistant', label: 'Assistant' },
              { href: '/account', label: 'Account' },
            ], user?.role).map(({ href, label, active }) => (
              <a
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  active
                    ? 'bg-prime text-prime-fg'
                    : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'
                }`}
              >
                {label}
              </a>
            ))}
          </nav>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher entities={entities} selectedId={selectedId} onSelect={setSelectedId} />
        )}

        {loading && !data && <Skeleton />}

        {/* Initial-load failure: no data to show, so surface the full error + retry. */}
        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load equity holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button
              onClick={handleRetry}
              className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors"
            >
              Retry
            </button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            {/* Background-refresh failure: keep the last-loaded table, show a quiet notice. */}
            {error && (
              <div role="status" className="mb-3 flex items-center justify-between gap-3 bg-card rounded-lg border border-rule px-4 py-2">
                <p className="text-xs text-ghost">Couldn’t refresh — showing last loaded values.</p>
                <button
                  onClick={handleRetry}
                  className="shrink-0 text-xs border border-wire text-dim px-2.5 py-1 rounded hover:border-dim hover:text-ink transition-colors"
                >
                  Retry
                </button>
              </div>
            )}
            {activity && <TradedToday data={activity} showEntityCol={showEntityCol} />}
            <EquityTable
              holdings={data.holdings}
              totals={data.totals}
              showEntityCol={showEntityCol}
            />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Live prices during market hours (09:15–15:30 IST)
        </p>
      </div>
    </main>
  );
}
