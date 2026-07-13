'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import NavTabs from '@/app/components/NavTabs';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import ForeignEquityTable, { type EquityHoldingRow, type EquityTotals, type CashCurrencyRow } from './components/ForeignEquityTable';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface ForeignEquityResponse {
  entity_id: number;
  entity_name: string;
  total_holdings: number;
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
  fx_rates: Record<string, number>;
  as_of_date: string | null;
  last_updated: string | null;
  cash_currency_breakdown?: CashCurrencyRow[];
}

interface ForeignActivityTrade {
  entity_name: string;
  broker: string;
  security_name: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  price_native: number;
  currency: string;
  value_inr: number | null;
  realized_pnl: number | null;
}
interface ForeignActivityResponse {
  date: string;
  buy_count: number;
  sell_count: number;
  realized_pnl_total: number;
  trades: ForeignActivityTrade[];
}

// Manually-entered foreign equity — Manual Data category "overseas_equity"
// (labelled "Foreign Equity" in the form). Values are already in INR.
interface ManualForeignAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  inception_date: string | null; notes: string | null;
}
interface ManualForeignResponse {
  category: string; entity_id: number; total_value: number; count: number; assets: ManualForeignAsset[];
}

const BROKER_LABEL: Record<string, string> = { ibkr: 'IBKR', vested: 'Vested', dbs: 'DBS' };

// Foreign trades booked today: IBKR exact Flex fills + Vested snapshot-diff (both from
// equity_trade_ledger). Native price shown in its own currency; value/P&L in INR.
function ForeignTradedToday({ data, showEntityCol }: { data: ForeignActivityResponse; showEntityCol: boolean }) {
  const [open, setOpen] = useState(true);
  if (!data.trades.length) return null;
  const inr = (v: number) => v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
  const num = (v: number) => v.toLocaleString('en-US', { maximumFractionDigits: 2 });
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
                <th className="px-3 py-2 text-left font-semibold">Broker</th>
                <th className="px-3 py-2 text-left font-semibold">Security</th>
                <th className="px-3 py-2 text-left font-semibold">Side</th>
                <th className="px-3 py-2 text-right font-semibold">Qty</th>
                <th className="px-3 py-2 text-right font-semibold">Rate</th>
                <th className="px-3 py-2 text-right font-semibold">Value (₹)</th>
                <th className="px-3 py-2 text-right font-semibold">Realised P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {data.trades.map((t, i) => {
                const sell = t.side === 'SELL';
                return (
                  <tr key={i} className="border-t border-rule">
                    {showEntityCol && <td className="px-3 py-2 text-dim">{t.entity_name}</td>}
                    <td className="px-3 py-2 text-ghost">{BROKER_LABEL[t.broker] ?? t.broker}</td>
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
                    <td className="px-3 py-2 text-right text-dim">{t.quantity.toLocaleString('en-US', { maximumFractionDigits: 4 })}</td>
                    <td className="px-3 py-2 text-right text-dim">{num(t.price_native)} {t.currency}</td>
                    <td className="px-3 py-2 text-right text-dim">{t.value_inr == null ? '—' : `₹${inr(t.value_inr)}`}</td>
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
      {[...Array(5)].map((_, i) => (
        <div key={i} className="flex gap-4 px-5 sm:px-6 py-3.5 border-t border-rule">
          <div className="h-3 bg-rule rounded w-16 animate-pulse" />
          <div className="h-3 bg-rule rounded w-20 animate-pulse" />
          <div className="flex-1 h-3 bg-rule rounded animate-pulse" />
        </div>
      ))}
    </div>
  );
}

function fmtAsOf(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n < 0 ? '−₹' : '₹') + Math.round(Math.abs(n)).toLocaleString('en-IN');
}

// Manually-tracked foreign equity, entered in Manual Data. Broker-synced holdings
// live in the table above; these are hand-entered positions (INR figures).
function ManualForeignEquity({ assets, showEntityCol }: { assets: ManualForeignAsset[]; showEntityCol: boolean }) {
  if (!assets.length) return null;
  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden mt-5">
      <div className="px-5 sm:px-6 pt-4 pb-3 border-b border-rule flex items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-ink">Manually-tracked Foreign Equity</h2>
        <a href="/manual-data" className="shrink-0 text-xs font-medium border border-rule text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
          + Add / edit in Manual Data
        </a>
      </div>
      <div className="p-4 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {assets.map(a => {
          const pnl = a.cost != null && a.current_value != null ? a.current_value - a.cost : null;
          return (
            <div key={`${a.entity_id}-${a.label}`} className="bg-page rounded-lg border border-rule p-4 flex flex-col gap-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
                  {showEntityCol && <p className="text-[11px] text-ghost mt-0.5">{a.entity_name}</p>}
                  {a.inception_date && <p className="text-[11px] text-ghost">Since {a.inception_date}</p>}
                </div>
                <div className="text-right shrink-0">
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Value</p>
                  <p className="text-base font-bold text-ink tabular-nums">{fmtINR(a.current_value)}</p>
                  {a.currency && a.currency !== 'INR' && <p className="text-[11px] text-ghost">{a.currency}</p>}
                </div>
              </div>
              <div className="flex items-center justify-between text-[11px] text-ghost">
                <span>Cost {fmtINR(a.cost)}</span>
                {pnl != null && (
                  <span style={{ color: pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }} className="font-medium tabular-nums">
                    {pnl >= 0 ? '+' : ''}{fmtINR(pnl)}
                  </span>
                )}
              </div>
              {a.notes && <p className="text-[11px] text-ghost border-t border-rule pt-2">{a.notes}</p>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function ForeignEquityPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [data, setData]             = useState<ForeignEquityResponse | null>(null);
  const [activity, setActivity]     = useState<ForeignActivityResponse | null>(null);
  const [manual, setManual]         = useState<ManualForeignResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);
  const didInitialLoad              = useRef(false);

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
    if (!didInitialLoad.current) setLoading(true);
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/foreign-equity/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load foreign equity holdings.'); return r.json(); })
      .then((d: ForeignEquityResponse | null) => {
        if (d) setData(d);
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selectedId, retryCount]);

  // Today's detected foreign trades (IBKR Flex fills + Vested snapshot diffs). Silent on failure.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/foreign-equity/activity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ForeignActivityResponse | null) => { if (d) setActivity(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selectedId, retryCount]);

  // Manually-entered foreign equity (Manual Data → "Foreign Equity" / overseas_equity).
  // Rolled into the headline totals and listed in its own section. Silent on failure.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedId !== null ? `&entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/manual-assets?category=overseas_equity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ManualForeignResponse | null) => { if (d) setManual(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selectedId, retryCount]);

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedId === null;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);

  // Fold manual foreign-equity entries into the headline totals (all INR).
  const manualAssets = manual?.assets ?? [];
  const manualExtra = manualAssets.length ? (() => {
    let cost = 0, value = 0, pnl = 0;
    for (const a of manualAssets) {
      cost  += a.cost ?? 0;
      value += a.current_value ?? 0;
      if (a.cost != null && a.current_value != null) pnl += a.current_value - a.cost;
    }
    return { cost, value, pnl, count: manualAssets.length };
  })() : undefined;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Foreign Equity</h1>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-sm text-ghost">International holdings (IBKR, Vested, DBS) in native currency</span>
              {data?.as_of_date && (
                <span className="text-xs text-ghost">· as of {fmtAsOf(data.as_of_date)}</span>
              )}
            </div>
          </div>
          <NavTabs active="/foreign-equity" role={user?.role} />
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/foreign-equity" entities={entities} selectedId={selectedId} onSelect={setSelectedId} />
        )}

        {loading && !data && <Skeleton />}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load foreign equity holdings</p>
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
            {activity && <ForeignTradedToday data={activity} showEntityCol={showEntityCol} />}
            <ForeignEquityTable
              holdings={data.holdings}
              totals={data.totals}
              fxRates={data.fx_rates}
              showEntityCol={showEntityCol}
              lastUpdated={data.last_updated}
              cashByCurrency={data.cash_currency_breakdown ?? []}
              extra={manualExtra}
            />
            <ManualForeignEquity assets={manualAssets} showEntityCol={showEntityCol} />
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Foreign holdings update on each broker sync
        </p>
      </div>
    </main>
  );
}
