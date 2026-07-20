'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import ForeignEquityTable, { type EquityHoldingRow, type EquityTotals, type CashCurrencyRow, type CashBrokerRow } from './components/ForeignEquityTable';
import { asOf, asOfDate } from '@/app/lib/asOf';

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
  cash_by_broker?: CashBrokerRow[];
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
// (labelled "Foreign Equity" in the form). cost/current_value are always INR;
// raw_amount is the figure as entered in the asset's own currency.
interface ManualForeignAsset {
  entity_id: number; entity_name: string; label: string;
  cost: number | null; current_value: number | null; currency: string;
  raw_amount: number | null; fx_rate: number | null;
  inception_date: string | null; notes: string | null;
  updated_at?: string | null;   // last time this figure was entered — see lib/asOf
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

// Same symbol table the Bank Accounts and holdings tables use.
const MANUAL_CCY_SYMBOL: Record<string, string> = {
  INR: '₹', USD: '$', EUR: '€', GBP: '£', CHF: 'CHF ', SGD: 'S$', AED: 'AED ', HKD: 'HK$',
};
function fmtNative(n: number | null | undefined, ccy: string): string {
  if (n == null) return '—';
  if (!ccy || ccy === 'INR') return fmtINR(n);
  const sym = MANUAL_CCY_SYMBOL[ccy] ?? ccy + ' ';
  return (n < 0 ? '−' : '') + sym +
    Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

// Manually-tracked foreign equity, entered in Manual Data. Broker-synced holdings
// live in the table above; these are hand-entered positions. A foreign-currency
// entry leads with the amount in its own currency (raw_amount, exactly as typed)
// and shows the INR conversion underneath — the headline totals stay INR.
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
          const ccy = a.currency || 'INR';
          const foreign = ccy !== 'INR';
          // Native cost has no stored counterpart — derive it from the same rate
          // that produced raw_amount, so cost and value are quoted on one basis.
          const costNative = foreign && a.fx_rate ? (a.cost != null ? a.cost / a.fx_rate : null) : a.cost;
          const showNative = foreign && a.raw_amount != null;
          const entered = asOf(a.updated_at);
          return (
            <div key={`${a.entity_id}-${a.label}`} className="bg-page rounded-lg border border-rule p-4 flex flex-col gap-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-sm font-semibold text-ink leading-tight">{a.label}</h3>
                  {showEntityCol && <p className="text-[11px] text-ghost mt-0.5">{a.entity_name}</p>}
                  {a.inception_date && <p className="text-[11px] text-ghost">Since {a.inception_date}</p>}
                  {entered && (
                    <p className="text-[11px] mt-0.5"
                       style={{ color: entered.stale ? 'var(--caution)' : 'var(--ghost)' }}
                       title={`Figure last entered on ${asOfDate(a.updated_at)}`}>
                      {entered.stale && '⚠ '}Entered {entered.label}
                    </p>
                  )}
                </div>
                <div className="text-right shrink-0">
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Value</p>
                  <p className="text-base font-bold text-ink tabular-nums">
                    {showNative ? fmtNative(a.raw_amount, ccy) : fmtINR(a.current_value)}
                  </p>
                  {showNative
                    ? <p className="text-[11px] text-ghost tabular-nums">{fmtINR(a.current_value)}</p>
                    : foreign && <p className="text-[11px] text-ghost">{ccy}</p>}
                </div>
              </div>
              <div className="flex items-center justify-between text-[11px] text-ghost">
                <span>Cost {showNative ? fmtNative(costNative, ccy) : fmtINR(a.cost)}</span>
                {pnl != null && (
                  <span style={{ color: pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }} className="font-medium tabular-nums">
                    {pnl >= 0 ? '+' : ''}
                    {showNative && costNative != null && a.raw_amount != null
                      ? fmtNative(a.raw_amount - costNative, ccy)
                      : fmtINR(pnl)}
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

// DBS Wealth has no API/scrape — the holdings statement is uploaded weekly as a
// CSV. Parse-then-confirm: /preview shows the extracted rows, /commit snapshot-
// replaces this entity's DBS holdings. Admin only.
interface DbsPreviewHolding {
  name: string; symbol: string; isin: string | null; exchange: string | null;
  currency: string; quantity: number; avg_cost_native: number | null;
  price_native: number | null; market_value_native: number | null; resolvable: boolean;
}
interface DbsPreviewResponse {
  entity_id: number; entity_name: string; committed: boolean;
  account: string | null; as_of: string | null; note: string | null;
  holdings: DbsPreviewHolding[];
  cash: { currency: string; market_value_native: number | null }[];
  replaced?: number; inserted?: number;
}

function DbsUploadCard({ entities, defaultEntityId, onCommitted }:
  { entities: Entity[]; defaultEntityId: number | null; onCommitted: () => void }) {
  const [open, setOpen] = useState(false);
  const [entityId, setEntityId] = useState<number | ''>(defaultEntityId ?? '');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<DbsPreviewResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => { if (defaultEntityId != null) setEntityId(defaultEntityId); }, [defaultEntityId]);

  const send = async (path: 'preview' | 'commit') => {
    if (!entityId || !file) { setErr('Pick an entity and a CSV file.'); return; }
    setBusy(true); setErr(null); setDone(null);
    try {
      const fd = new FormData();
      fd.append('entity_id', String(entityId));
      fd.append('file', file);
      const r = await fetch(`${API_URL}/api/v1/foreign-equity/dbs/${path}`,
        { method: 'POST', credentials: 'include', body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `Upload failed (${r.status})`);
      if (path === 'preview') { setPreview(d); }
      else {
        setDone(`Saved — replaced ${d.replaced ?? 0}, wrote ${d.inserted ?? 0} holding(s) as of ${d.as_of}.`);
        setPreview(null); setFile(null); onCommitted();
      }
    } catch (e) { setErr(e instanceof Error ? e.message : 'Upload failed.'); }
    finally { setBusy(false); }
  };

  const num = (v: number | null) => v == null ? '—' : v.toLocaleString('en-US', { maximumFractionDigits: 2 });

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden mt-5">
      <button onClick={() => setOpen(o => !o)} aria-expanded={open}
        className="w-full flex items-center justify-between px-5 sm:px-6 py-3 text-left hover:bg-page transition-colors">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-ink">Upload DBS statement</span>
          <span className="text-xs text-ghost">weekly holdings CSV · snapshot-replace</span>
        </div>
        <span className="text-xs text-dim">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="border-t border-rule p-4 sm:p-5 space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] uppercase tracking-wide text-ghost">Entity</span>
              <select value={entityId} onChange={e => setEntityId(e.target.value ? Number(e.target.value) : '')}
                className="bg-page border border-rule rounded px-3 py-1.5 text-sm text-ink">
                <option value="">Select…</option>
                {entities.map(en => <option key={en.id} value={en.id}>{en.name}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[11px] uppercase tracking-wide text-ghost">DBS holdings CSV</span>
              <input type="file" accept=".csv,text/csv"
                onChange={e => { setFile(e.target.files?.[0] ?? null); setPreview(null); setDone(null); }}
                className="text-sm text-dim file:mr-3 file:py-1.5 file:px-3 file:rounded file:border file:border-rule file:bg-page file:text-ink file:text-xs" />
            </label>
            <button onClick={() => send('preview')} disabled={busy || !file || !entityId}
              className="text-xs font-medium border border-rule text-dim px-3 py-2 rounded hover:border-dim hover:text-ink transition-colors disabled:opacity-40">
              {busy ? 'Working…' : 'Preview'}
            </button>
          </div>

          {err && <p role="alert" className="text-xs" style={{ color: 'var(--peril)' }}>{err}</p>}
          {done && <p role="status" className="text-xs" style={{ color: 'var(--gain)' }}>{done}</p>}

          {preview && (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-ghost">
                  {preview.entity_name} · account {preview.account ?? '—'} · as of {preview.as_of ?? '—'} · {preview.note}
                </p>
                <button onClick={() => send('commit')} disabled={busy}
                  className="text-xs font-semibold border border-rule px-3 py-1.5 rounded hover:bg-page transition-colors disabled:opacity-40"
                  style={{ color: 'var(--gain)' }}>
                  Confirm &amp; replace {preview.holdings.length} holding(s)
                </button>
              </div>
              <div className="overflow-x-auto border border-rule rounded">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="bg-page text-dim">
                      <th className="px-3 py-2 text-left font-semibold">Symbol</th>
                      <th className="px-3 py-2 text-left font-semibold">Name</th>
                      <th className="px-3 py-2 text-left font-semibold">Ccy</th>
                      <th className="px-3 py-2 text-right font-semibold">Qty</th>
                      <th className="px-3 py-2 text-right font-semibold">Avg cost</th>
                      <th className="px-3 py-2 text-right font-semibold">Price</th>
                      <th className="px-3 py-2 text-right font-semibold">Mkt value</th>
                      <th className="px-3 py-2 text-left font-semibold">Price feed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.holdings.map((h, i) => (
                      <tr key={i} className="border-t border-rule">
                        <td className="px-3 py-2 text-ink font-medium">{h.symbol}</td>
                        <td className="px-3 py-2 text-dim">{h.name}</td>
                        <td className="px-3 py-2 text-ghost">{h.currency}</td>
                        <td className="px-3 py-2 text-right text-dim">{num(h.quantity)}</td>
                        <td className="px-3 py-2 text-right text-dim">{num(h.avg_cost_native)}</td>
                        <td className="px-3 py-2 text-right text-dim">{num(h.price_native)}</td>
                        <td className="px-3 py-2 text-right text-dim">{num(h.market_value_native)}</td>
                        <td className="px-3 py-2 text-[11px]" style={{ color: h.resolvable ? 'var(--gain)' : 'var(--ghost)' }}>
                          {h.resolvable ? 'live' : 'statement value'}
                        </td>
                      </tr>
                    ))}
                    {preview.cash.map((c, i) => (
                      <tr key={`c${i}`} className="border-t border-rule bg-page/50">
                        <td className="px-3 py-2 text-ghost">CASH</td>
                        <td className="px-3 py-2 text-ghost" colSpan={2}>{c.currency} cash balance</td>
                        <td className="px-3 py-2 text-right text-ghost" colSpan={4}>{num(c.market_value_native)} {c.currency}</td>
                        <td className="px-3 py-2 text-[11px] text-ghost">→ broker cash</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="text-[11px] text-ghost">
                Confirming replaces this entity’s entire DBS holding set — names absent from this file are treated as exited.
                Cash balances are recorded to broker cash (per-currency), swept currencies dropped.
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ForeignEquityPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
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
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/foreign-equity/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load foreign equity holdings.'); return r.json(); })
      .then((d: ForeignEquityResponse | null) => {
        if (d) setData(d);
        setLoading(false);
        didInitialLoad.current = true;
      })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Today's detected foreign trades (IBKR Flex fills + Vested snapshot diffs). Silent on failure.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/foreign-equity/activity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ForeignActivityResponse | null) => { if (d) setActivity(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Manually-entered foreign equity (Manual Data → "Foreign Equity" / overseas_equity).
  // Rolled into the headline totals and listed in its own section. Silent on failure.
  useEffect(() => {
    const controller = new AbortController();
    const qs = selectedIds.length ? '&' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/manual-assets?category=overseas_equity${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => (r.ok ? r.json() : null))
      .then((d: ManualForeignResponse | null) => { if (d) setManual(d); })
      .catch(() => {});
    return () => controller.abort();
  }, [selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedIds.length !== 1;
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
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/foreign-equity" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
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
              cashByBroker={data.cash_by_broker ?? []}
              extra={manualExtra}
            />
            <ManualForeignEquity assets={manualAssets} showEntityCol={showEntityCol} />
            {isAdmin && entities.length > 0 && (
              <DbsUploadCard
                entities={entities}
                defaultEntityId={selectedIds.length === 1 ? selectedIds[0] : null}
                onCommitted={handleRetry}
              />
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          Rajani MIS &copy; {new Date().getFullYear()} · Foreign holdings update on each broker sync
        </p>
      </div>
    </main>
  );
}
