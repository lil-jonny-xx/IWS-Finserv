'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import EntitySwitcher from '@/app/components/EntitySwitcher';
import { useDragScroll } from '@/app/components/DragScroll';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; email?: string; entity_id?: number; }
interface Entity { id: number; name: string; }

interface PmsHolding {
  entity_id: number;
  entity_name: string;
  source: string;
  source_label: string;
  holding_type: 'equity' | 'cash';
  security_name: string;
  isin: string | null;
  quantity: number | null;
  avg_cost: number | null;
  cost: number | null;
  current_price: number | null;
  market_value: number;
  weight_pct: number | null;
  pnl: number | null;
  returns_pct: number | null;
}
interface PmsAgg {
  equity_total: number;
  cash_total: number;
  total: number;
  equity_cost: number;
  invested_cost: number;
  equity_pnl: number | null;
  returns_pct: number | null;
  cost_complete: boolean;
  equity_count: number;
  cash_count: number;
}
interface PmsBySource extends PmsAgg {
  entity_id: number;
  entity_name: string;
  source: string;
  source_label: string;
  as_on_date: string | null;
  xirr_pct: number | null;
  deposit_total: number | null;
}
interface PmsByEntity extends PmsAgg {
  entity_id: number;
  entity_name: string;
  pms_count: number;
}
interface PmsResponse {
  entity_id: number;
  entity_name: string;
  as_on_date: string | null;
  totals: PmsAgg;
  by_entity: PmsByEntity[];
  by_source: PmsBySource[];
  holdings: PmsHolding[];
}

const inr = (n: number | null | undefined) =>
  n == null ? '—' : n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
const qty = (n: number | null | undefined) =>
  n == null ? '—' : n.toLocaleString('en-IN', { maximumFractionDigits: 2 });
const signedInr = (n: number | null | undefined) =>
  n == null ? '—' : (n < 0 ? '−₹' : '₹') + inr(Math.abs(n));
const pct = (n: number | null | undefined) =>
  n == null ? '—' : `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
const pnlClass = (n: number | null | undefined) =>
  n == null ? 'text-ghost' : n >= 0 ? 'text-gain' : 'text-peril';


function TotalCard({ label, value, sub, subClass }: {
  label: string; value: string; sub?: string; subClass?: string;
}) {
  return (
    <div className="bg-card rounded-lg border border-rule px-5 py-4">
      <p className="text-xs text-ghost">{label}</p>
      <p className="text-xl font-bold mt-1 text-ink">{value}</p>
      {sub && <p className={`text-xs mt-0.5 ${subClass ?? 'text-ghost'}`}>{sub}</p>}
    </div>
  );
}

function EntityMetricsTable({ rows }: { rows: PmsByEntity[] }) {
  const ds = useDragScroll();
  const sum = (f: (r: PmsByEntity) => number) => rows.reduce((s, r) => s + f(r), 0);
  const totalInvested = sum(r => r.invested_cost);
  const totalValue    = sum(r => r.total);
  const totalPnl      = rows.some(r => r.equity_pnl != null)
    ? sum(r => r.equity_pnl ?? 0) : null;
  const totalCost     = sum(r => r.equity_cost);
  const totalRet      = totalPnl != null && totalCost > 0 ? (totalPnl / totalCost) * 100 : null;
  return (
    <div ref={ds.ref} {...ds.bind} className="bg-card rounded-lg border border-rule overflow-x-auto mb-5">
      <table className="w-full min-w-[720px]">
        <thead>
          <tr className="text-left text-xs text-ghost">
            <th className="px-5 py-3 font-medium">Entity</th>
            <th className="px-5 py-3 font-medium text-right">PMS Accounts</th>
            <th className="px-5 py-3 font-medium text-right">Invested (Cost + Cash)</th>
            <th className="px-5 py-3 font-medium text-right">Cash</th>
            <th className="px-5 py-3 font-medium text-right">Current Value</th>
            <th className="px-5 py-3 font-medium text-right">P&L</th>
            <th className="px-5 py-3 font-medium text-right">Return</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.entity_id} className="border-t border-rule hover:bg-page">
              <td className="px-5 py-2.5 text-sm text-ink">
                {r.entity_name}
                {!r.cost_complete && <span className="text-xs text-ghost" title="One or more PMS providers don't report cost — P&L covers costed holdings only."> *</span>}
              </td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{r.pms_count}</td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(r.invested_cost)}</td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(r.cash_total)}</td>
              <td className="px-5 py-2.5 text-sm text-ink font-medium text-right tabular-nums">₹{inr(r.total)}</td>
              <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(r.equity_pnl)}`}>{signedInr(r.equity_pnl)}</td>
              <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(r.returns_pct)}`}>{pct(r.returns_pct)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-rule bg-card font-semibold">
            <td className="px-5 py-2.5 text-xs text-dim uppercase tracking-wide">All entities</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{sum(r => r.pms_count)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(totalInvested)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">₹{inr(sum(r => r.cash_total))}</td>
            <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">₹{inr(totalValue)}</td>
            <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(totalPnl)}`}>{signedInr(totalPnl)}</td>
            <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(totalRet)}`}>{pct(totalRet)}</td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

function SourceStat({ label, value, valueClass, title }: {
  label: string; value: string; valueClass?: string; title?: string;
}) {
  return (
    <div title={title}>
      <p className="text-xs text-ghost mb-0.5">{label}</p>
      <p className={`text-sm font-semibold tabular-nums ${valueClass ?? 'text-ink'}`}>{value}</p>
    </div>
  );
}

function SourceHoldingsTable({ rows }: { rows: PmsHolding[] }) {
  const ds = useDragScroll();
  const equity = rows.filter(r => r.holding_type === 'equity');
  const cash   = rows.filter(r => r.holding_type === 'cash');

  const section = (title: string, items: PmsHolding[]) => {
    if (items.length === 0) return null;
    const costed  = items.filter(h => h.cost != null);
    const costSub = costed.length > 0 ? costed.reduce((s, h) => s + (h.cost ?? 0), 0) : null;
    const mvSub   = items.reduce((s, h) => s + h.market_value, 0);
    const pnlSub  = title === 'Equity' && costed.length > 0
      ? costed.reduce((s, h) => s + (h.pnl ?? 0), 0) : null;
    const retSub  = pnlSub != null && costSub != null && costSub > 0 ? (pnlSub / costSub) * 100 : null;
    const qtySub  = items.some(h => h.quantity != null)   ? items.reduce((s, h) => s + (h.quantity ?? 0), 0)   : null;
    const wSub    = items.some(h => h.weight_pct != null) ? items.reduce((s, h) => s + (h.weight_pct ?? 0), 0) : null;
    return (
      <>
        <tr className="bg-page">
          <td colSpan={8} className="px-5 py-2 text-xs font-semibold text-dim uppercase tracking-wide">
            {title}
          </td>
        </tr>
        {items.map((h, i) => (
          <tr key={`${title}-${i}`} className="border-t border-rule hover:bg-page">
            <td className="px-5 py-2.5 text-sm text-ink">{h.security_name}</td>
            <td className="px-5 py-2.5 text-xs text-ghost">{h.isin ?? '—'}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{qty(h.quantity)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{inr(h.cost)}</td>
            <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{inr(h.market_value)}</td>
            <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(h.pnl)}`}>{signedInr(h.pnl)}</td>
            <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(h.returns_pct)}`}>{pct(h.returns_pct)}</td>
            <td className="px-5 py-2.5 text-sm text-right tabular-nums text-dim">
              {h.weight_pct == null ? '—' : `${h.weight_pct.toFixed(1)}%`}
            </td>
          </tr>
        ))}
        <tr className="border-t border-rule bg-card font-medium">
          <td colSpan={2} className="px-5 py-2.5 text-xs text-dim">{title} total</td>
          <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{qty(qtySub)}</td>
          <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{inr(costSub)}</td>
          <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">{inr(mvSub)}</td>
          <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(pnlSub)}`}>{signedInr(pnlSub)}</td>
          <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(retSub)}`}>{pct(retSub)}</td>
          <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{wSub == null ? '—' : `${wSub.toFixed(1)}%`}</td>
        </tr>
      </>
    );
  };

  // Grand total across Equity + Cash — the "total everything down" bottom bar.
  const gCosted = rows.filter(h => h.cost != null);
  const gCost   = gCosted.length > 0 ? gCosted.reduce((s, h) => s + (h.cost ?? 0), 0) : null;
  const gMv     = rows.reduce((s, h) => s + h.market_value, 0);
  const gQty    = rows.some(h => h.quantity != null)   ? rows.reduce((s, h) => s + (h.quantity ?? 0), 0)   : null;
  const gPnl    = rows.some(h => h.pnl != null)        ? rows.filter(h => h.pnl != null).reduce((s, h) => s + (h.pnl ?? 0), 0) : null;
  const gRet    = gPnl != null && gCost != null && gCost > 0 ? (gPnl / gCost) * 100 : null;
  const gW      = rows.some(h => h.weight_pct != null) ? rows.reduce((s, h) => s + (h.weight_pct ?? 0), 0) : null;

  return (
    <div ref={ds.ref} {...ds.bind} className="bg-card rounded-lg border border-rule overflow-x-auto">
      <table className="w-full min-w-[820px]">
        <thead>
          <tr className="text-left text-xs text-ghost">
            <th className="px-5 py-3 font-medium">Security</th>
            <th className="px-5 py-3 font-medium">ISIN</th>
            <th className="px-5 py-3 font-medium text-right">Qty</th>
            <th className="px-5 py-3 font-medium text-right">Cost</th>
            <th className="px-5 py-3 font-medium text-right">Market Value</th>
            <th className="px-5 py-3 font-medium text-right">P&L</th>
            <th className="px-5 py-3 font-medium text-right">Return</th>
            <th className="px-5 py-3 font-medium text-right">Weight</th>
          </tr>
        </thead>
        <tbody>
          {section('Equity', equity)}
          {section('Cash', cash)}
          {rows.length > 0 && (
            <tr className="border-t-2 border-rule bg-page font-semibold">
              <td colSpan={2} className="px-5 py-2.5 text-xs text-dim uppercase tracking-wide">Total ({rows.length} holdings)</td>
              <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">{qty(gQty)}</td>
              <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">{inr(gCost)}</td>
              <td className="px-5 py-2.5 text-sm text-ink text-right tabular-nums">{inr(gMv)}</td>
              <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(gPnl)}`}>{signedInr(gPnl)}</td>
              <td className={`px-5 py-2.5 text-sm text-right tabular-nums ${pnlClass(gRet)}`}>{pct(gRet)}</td>
              <td className="px-5 py-2.5 text-sm text-dim text-right tabular-nums">{gW == null ? '—' : `${gW.toFixed(1)}%`}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// Display overrides for PMS providers: user-facing product name + portfolio
// manager, keyed on the backend source_label ("Nuvama"/"Zerodha"/"ICICI Pru").
const PMS_DISPLAY: Record<string, { title: string; manager?: string }> = {
  Nuvama:  { title: 'Prudent Investment', manager: 'Prashasta Seth' },
  Zerodha: { title: 'Zerodha',            manager: 'Vijay Thakkar' },
};

function SourceSection({ s, holdings, showEntity }: {
  s: PmsBySource; holdings: PmsHolding[]; showEntity: boolean;
}) {
  const disp = PMS_DISPLAY[s.source_label] ?? { title: s.source_label };
  return (
    <section className="mb-8">
      <div className="flex flex-wrap items-baseline justify-between gap-2 mb-3">
        <div>
          <h2 className="text-lg font-semibold text-ink">
            {showEntity ? `${s.entity_name} · ` : ''}{disp.title} PMS
          </h2>
          {disp.manager && (
            <p className="text-xs text-ghost mt-0.5">Managed by {disp.manager}</p>
          )}
          <p className="text-xs text-ghost mt-0.5">
            {s.equity_count} holding{s.equity_count === 1 ? '' : 's'}
            {s.as_on_date ? ` · as on ${s.as_on_date}` : ''}
            {s.deposit_total != null && ` · return on invested ₹${inr(s.equity_cost)} (₹${inr(s.deposit_total)} deposited − ₹${inr(s.cash_total)} cash leftover)`}
            {!s.cost_complete && ' · cost basis not reported by provider — P&L unavailable'}
          </p>
        </div>
      </div>
      <div className="bg-card rounded-lg border border-rule px-5 py-3.5 mb-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-x-6 gap-y-3">
        <SourceStat label="Invested (Cost + Cash)" value={`₹${inr(s.cost_complete ? s.invested_cost : null)}`} />
        <SourceStat label="Current Value" value={`₹${inr(s.total)}`} />
        <SourceStat label="Cash" value={`₹${inr(s.cash_total)}`} />
        <SourceStat label="P&L" value={signedInr(s.equity_pnl)} valueClass={pnlClass(s.equity_pnl)} />
        <SourceStat label="Return" value={pct(s.returns_pct)} valueClass={pnlClass(s.returns_pct)}
          title="Absolute return on cost (market value vs cost basis)" />
        <SourceStat label="XIRR" value={s.xirr_pct == null ? '—' : `${pct(s.xirr_pct)} p.a.`} valueClass={pnlClass(s.xirr_pct)}
          title="Money-weighted annual return from actual deposits & withdrawals in the broker ledger. Only available where the funding ledger is imported (Zerodha PMS)." />
      </div>
      <SourceHoldingsTable rows={holdings} />
    </section>
  );
}

export default function PmsPage() {
  const router = useRouter();
  const [user, setUser]             = useState<User | null>(null);
  const [entities, setEntities]     = useState<Entity[]>([]);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);   // empty = All; >1 = subset
  const selKey = selectedIds.join(',');
  const toggleEntity = useCallback((id: number | null) => {
    if (id === null) { setSelectedIds([]); return; }
    setSelectedIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);
  const [data, setData]             = useState<PmsResponse | null>(null);
  const [loading, setLoading]       = useState(true);
  const [error, setError]           = useState<string | null>(null);
  const [retryCount, setRetryCount] = useState(0);

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
    setLoading(true);
    setError(null);
    const qs = selectedIds.length ? '?' + selectedIds.map(id => `entity_id=${id}`).join('&') : '';
    fetch(`${API_URL}/api/v1/pms/holdings${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load PMS holdings.'); return r.json(); })
      .then((d: PmsResponse | null) => { if (d) setData(d); setLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, selKey, retryCount]);   // eslint-disable-line react-hooks/exhaustive-deps

  const isAdmin       = !!user;  // members have admin-level view access (only Manual Data + user mgmt are admin-only)
  const showEntityCol = isAdmin && selectedIds.length !== 1;
  const handleRetry   = useCallback(() => setRetryCount(c => c + 1), []);
  const t = data?.totals;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">

        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">PMS Portfolio</h1>
            <p className="text-sm text-ghost mt-0.5">
              PMS holdings by provider{data?.as_on_date ? ` · as on ${data.as_on_date}` : ''}
            </p>
          </div>
        </div>

        {isAdmin && entities.length > 0 && (
          <EntitySwitcher section="/pms" entities={entities} selectedIds={selectedIds} onToggle={toggleEntity} />
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-10 text-center text-sm text-ghost animate-pulse">
            Loading PMS holdings…
          </div>
        )}

        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load PMS holdings</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={handleRetry} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">
              Retry
            </button>
          </div>
        )}

        {data && t && (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-5">
              <TotalCard label="Total Invested (Cost + Cash)" value={`₹${inr(t.invested_cost)}`}
                sub={`Cost ₹${inr(t.equity_cost)} + Cash ₹${inr(t.cash_total)}`} />
              <TotalCard label="Equity" value={`₹${inr(t.equity_total)}`}
                sub={`${t.equity_count} holding${t.equity_count === 1 ? '' : 's'} · P&L ${signedInr(t.equity_pnl)}`}
                subClass={pnlClass(t.equity_pnl)} />
              <TotalCard label="Return on Cost" value={pct(t.returns_pct)}
                sub={t.cost_complete ? undefined : 'Over holdings with reported cost'}
                subClass="text-ghost" />
              <TotalCard label="Total (Equity + Cash)" value={`₹${inr(t.total)}`}
                sub={`Cash ₹${inr(t.cash_total)}`} />
            </div>

            {showEntityCol && data.by_entity.length > 1 && (
              <EntityMetricsTable rows={data.by_entity} />
            )}

            {data.by_source.length === 0 ? (
              <div className="bg-card rounded-lg border border-rule px-5 py-10 text-center text-sm text-ghost">
                No PMS holdings yet. They appear here once a PMS sync has run.
              </div>
            ) : (
              data.by_source.map(s => (
                <SourceSection
                  key={`${s.entity_id}-${s.source}`}
                  s={s}
                  holdings={data.holdings.filter(h => h.entity_id === s.entity_id && h.source === s.source)}
                  showEntity={showEntityCol}
                />
              ))
            )}
          </>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          Rajani MIS &copy; {new Date().getFullYear()} · PMS data sourced from Prudent Investment (Nuvama WealthSpectrum), Zerodha and ICICI Prudential
        </p>
      </div>
    </main>
  );
}
