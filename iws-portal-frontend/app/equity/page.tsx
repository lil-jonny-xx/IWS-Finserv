'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import DividendsCard from '@/app/components/DividendsCard';
import EquityTable, { type EquityHoldingRow, type EquityTotals, type CashBrokerRow } from './components/EquityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface EquityResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
  cash_by_broker?: CashBrokerRow[];
  // Non-API demat positions (SBI Securities, …) reconstructed from the manual trade
  // register and Kite-priced. Rendered in their own section; grand_totals combines
  // both sections (+ cash) for the true page-level portfolio figure.
  manual_holdings?: EquityHoldingRow[];
  manual_totals?: EquityTotals;
  grand_totals?: EquityTotals;
}

// One row per entity + security + side — the API folds a day's fills together, so
// `quantity` is the day's total, `price` the quantity-weighted rate, and `fills` how
// many individual fills went into it.
interface ActivityTrade {
  entity_id?: number;
  entity_name: string;
  security_name: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price: number;
  amount: number;
  realized_pnl: number | null;
  fills?: number;
}
interface ActivityResponse {
  date: string;
  buy_count: number;
  sell_count: number;
  realized_pnl_total: number;
  trades: ActivityTrade[];
}

// A fill pushed live over SSE by the live_trade_daemon (via /api/v1/live/trades).
interface LiveFill {
  entity: string;
  entity_id: number;
  broker: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  qty: number;
  price: number;
  amount: number;
  date: string;
  order_id: string;
}

// Today's trades, one row per security + side rather than one per fill — a position
// worked in ten fills is one decision. Qty is the day's total and Rate the
// quantity-weighted average across the fills, so it need not be a price that ever
// traded. Buys and sells stay separate rows and are never netted.
//
// Rows sourced from the snapshot differ (source='snapshot') are priced at the
// snapshot LTP at detection rather than the exact fill — see the API docstring.
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
                <th className="px-3 py-2 text-right font-semibold">Avg rate</th>
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
                    <td className="px-3 py-2 text-ink">
                      {t.security_name}
                      {/* Only worth saying when fills were actually folded together —
                          it explains why the rate is not a price that ever traded. */}
                      {(t.fills ?? 1) > 1 && (
                        <span className="ml-1.5 text-[10px] text-ghost">{t.fills} fills</span>
                      )}
                    </td>
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
  // Multi-entity selection; empty = All. Single-select still server-filters (one
  // entity_id, repeatable); empty fetches all. The backend scopes with = ANY(),
  // so totals already reflect the chosen subset — no client-side entity filter.
  const [selectedIds, setSelectedIds]     = useState<number[]>([]);
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }        // "All" clears
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]                   = useState<EquityResponse | null>(null);
  const [activity, setActivity]           = useState<ActivityResponse | null>(null);
  const [loading, setLoading]             = useState(true);
  const [error, setError]                 = useState<string | null>(null);
  const [retryCount, setRetryCount]       = useState(0);
  const [lastUpdated, setLastUpdated]     = useState<Date | null>(null);
  const [liveActive, setLiveActive]       = useState(false);
  const [sseLive, setSseLive]             = useState(false);
  const intervalRef                       = useRef<ReturnType<typeof setInterval> | null>(null);
  const refetchTimer                      = useRef<ReturnType<typeof setTimeout> | null>(null);
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
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/equity/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load equity holdings.'); return r.json(); })
      .then((d: EquityResponse | null) => {
        if (d) { setData(d); setLastUpdated(new Date()); }
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);

  // Today's detected buys/sells (snapshot worker). Same scope + refresh cadence as
  // holdings; failures are silent so they never disturb the main table.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/equity/activity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ActivityResponse | null) => { if (d) setActivity(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selKey, retryCount]);

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
  }, [selKey]);

  // Sub-second live fills via SSE (live_trade_daemon → Redis → /api/v1/live/trades).
  // Each fill is shown in "Traded today" instantly (optimistic), then a debounced
  // refetch pulls the authoritative holdings + activity (real realised P&L, updated
  // quantities). EventSource auto-reconnects on drop; events for other entities are
  // filtered out client-side when a single entity is selected.
  useEffect(() => {
    if (!user) return;
    const es = new EventSource(`${API_URL}/api/v1/live/trades`, { withCredentials: true });
    es.onopen = () => setSseLive(true);
    es.onerror = () => setSseLive(false);  // browser retries automatically
    es.onmessage = (e) => {
      let ev: LiveFill;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (!ev || !ev.symbol) return;
      if (selectedIds.length && !selectedIds.includes(ev.entity_id)) return;
      setActivity(prev => {
        const trade: ActivityTrade = {
          entity_id: ev.entity_id, entity_name: ev.entity, security_name: ev.symbol,
          side: ev.side, quantity: ev.qty, price: ev.price, amount: ev.amount,
          realized_pnl: null, fills: 1,
        };
        const isBuy = ev.side === 'BUY';
        if (!prev) return {
          date: ev.date, buy_count: isBuy ? 1 : 0, sell_count: isBuy ? 0 : 1,
          realized_pnl_total: 0, trades: [trade],
        };
        // Fold the fill into its existing group, mirroring how the API groups, so the
        // optimistic row doesn't briefly break the one-row-per-decision rule before
        // the debounced refetch lands. A new group moves to the top; an existing one
        // stays put, so a stock being worked doesn't jump around while you read it.
        const i = prev.trades.findIndex(
          t => t.security_name === ev.symbol && t.side === ev.side && t.entity_name === ev.entity,
        );
        if (i === -1) {
          return {
            ...prev,
            buy_count: prev.buy_count + (isBuy ? 1 : 0),
            sell_count: prev.sell_count + (isBuy ? 0 : 1),
            trades: [trade, ...prev.trades],
          };
        }
        const g   = prev.trades[i];
        const qty = g.quantity + ev.qty;
        const merged: ActivityTrade = {
          ...g,
          quantity: qty,
          // Re-weight rather than take the latest fill's price.
          price: qty ? (g.price * g.quantity + ev.price * ev.qty) / qty : g.price,
          amount: g.amount + ev.amount,
          fills: (g.fills ?? 1) + 1,
        };
        const trades = [...prev.trades];
        trades[i] = merged;
        // Counts are of groups, so folding into one leaves them unchanged.
        return { ...prev, trades };
      });
      // Debounced authoritative reconcile (real realised P&L + quantities from the API).
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
      refetchTimer.current = setTimeout(() => setRetryCount(c => c + 1), 1500);
    };
    return () => {
      es.close();
      setSseLive(false);
      if (refetchTimer.current) clearTimeout(refetchTimer.current);
    };
  }, [user, selKey]);

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedIds.length !== 1;   // show entity col for All or a multi-entity subset
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Equity Portfolio</h1>
            <div className="flex items-center gap-2 mt-0.5">
              {sseLive ? (
                <span className="flex items-center gap-1.5 text-xs text-gain font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-gain animate-pulse inline-block" />
                  Live · real-time fills
                </span>
              ) : liveActive ? (
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
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/equity" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
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
            <div className="mb-5">
              <DividendsCard scope="domestic" entityIds={selectedIds} />
            </div>
            {activity && <TradedToday data={activity} showEntityCol={showEntityCol} />}

            {/* Manual positions — non-API demats (SBI Securities, off-market) entered in
                the Trade Register and live-priced via Zerodha Kite. Kept in their own
                section so they're never confused with the broker-fed holdings below, but
                folded into the portfolio total via grand_totals. */}
            {data.manual_holdings && data.manual_holdings.length > 0 && data.manual_totals && (
              <div className="mb-6">
                <div className="mb-2">
                  <h2 className="text-sm font-semibold text-ink">Manual positions</h2>
                  <p className="text-xs text-ghost">
                    Off-market / non-API demats — reconstructed from the trade register, priced live via Zerodha
                  </p>
                </div>
                <EquityTable
                  holdings={data.manual_holdings}
                  totals={data.manual_totals}
                  cashByBroker={[]}
                  showEntityCol={showEntityCol}
                />
              </div>
            )}

            {data.manual_holdings && data.manual_holdings.length > 0 && (
              <h2 className="text-sm font-semibold text-ink mb-2">Holdings</h2>
            )}
            <EquityTable
              holdings={data.holdings}
              totals={data.totals}
              cashByBroker={data.cash_by_broker ?? []}
              showEntityCol={showEntityCol}
            />

            {/* Grand total across broker-fed holdings + manual positions + cash — shown
                only when there are manual positions, otherwise the table's own total is
                already the whole picture. */}
            {data.manual_holdings && data.manual_holdings.length > 0 && data.grand_totals && (
              <div className="mt-4 flex flex-wrap items-center justify-end gap-x-6 gap-y-1 bg-card rounded-lg border border-rule px-5 py-3">
                <span className="text-xs text-ghost">Total equity — all demats + cash</span>
                <span className="text-sm font-semibold text-ink">
                  ₹{(data.grand_totals.value_plus_cash ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </span>
              </div>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          Rajani MIS &copy; {new Date().getFullYear()} · Live prices during market hours (09:15–15:30 IST)
        </p>
      </div>
    </main>
  );
}
