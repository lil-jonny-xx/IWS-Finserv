'use client';
import { useState, useMemo } from 'react';
import DragScroll from '@/app/components/DragScroll';

export interface EquityHoldingRow {
  id: number;
  entity_id: number;
  entity_name?: string;
  broker: string;
  symbol: string;
  isin?: string;
  exchange?: string;
  sector?: string;
  quantity: number;
  avg_cost: number;
  cost: number;
  current_price?: number;
  current_market_value?: number;
  currency?: string;
  fx_rate?: number;
  avg_cost_native?: number;
  cost_native?: number;
  current_price_native?: number;
  current_market_value_native?: number;
  exposure_pct?: number;
  pnl_ytd?: number;
  pnl_inception?: number;
  returns_ytd_pct?: number;
  returns_inception_pct?: number;
  cagr_inception_pct?: number;
  xirr_inception_pct?: number;
  first_invested_date?: string;
  as_of_date?: string;
  remarks?: string;
}

export interface EquityTotals {
  total_cost?: number;
  total_current_market_value?: number;
  total_pnl_inception?: number;
  total_pnl_ytd?: number;
  cash_balance?: number;
  value_plus_cash?: number;
}

interface Props {
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
  fxRates: Record<string, number>;
  showEntityCol: boolean;
  lastUpdated?: string | null;
}

// ── currency ────────────────────────────────────────────────────────────────

const CCY_SYMBOL: Record<string, string> = {
  INR: '₹', USD: '$', CHF: 'CHF ', GBP: '£', SGD: 'S$', AED: 'AED ', HKD: 'HK$',
};
// Order in which switcher pills appear (after Native + INR).
const SWITCH_CCYS = ['USD', 'CHF', 'GBP', 'SGD', 'AED', 'HKD'];

type Display = 'native' | string;

function fmtMoney(n: number | null | undefined, ccy: string, frac = 0): string {
  if (n == null) return '—';
  const sym    = CCY_SYMBOL[ccy] ?? ccy + ' ';
  const locale = ccy === 'INR' ? 'en-IN' : 'en-US';
  return (n < 0 ? '−' : '') + sym +
    Math.abs(n).toLocaleString(locale, { maximumFractionDigits: frac, minimumFractionDigits: frac ? 2 : 0 });
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function fmtDate(iso: string | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

// ── constants ─────────────────────────────────────────────────────────────────

const BROKER_LABELS: Record<string, string> = {
  ibkr:   'Interactive Brokers',
  vested: 'Vested',
  dbs:    'DBS Wealth',
};
const BROKER_COLORS: Record<string, string> = {
  ibkr:   '#d2122e',
  vested: '#7c3aed',
  dbs:    '#b8860b',
};

type SortKey = keyof EquityHoldingRow;
type SortDir = 'asc' | 'desc';

function sortRows(rows: EquityHoldingRow[], key: SortKey, dir: SortDir): EquityHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = (a[key] as number | string) ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = (b[key] as number | string) ?? (typeof b[key] === 'number' ? -Infinity : '');
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function FilterPills({
  label, options, labelMap, selected, onChange,
}: {
  label: string; options: string[]; labelMap?: Record<string, string>;
  selected: string | null; onChange: (v: string | null) => void;
}) {
  if (options.length < 2) return null;
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] text-ghost font-medium shrink-0">{label}:</span>
      {[{ value: null, label: 'All' }, ...options.map(o => ({ value: o, label: labelMap?.[o] ?? o }))].map(opt => (
        <button
          key={opt.value ?? 'all'}
          onClick={() => onChange(opt.value === selected ? null : opt.value)}
          className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors whitespace-nowrap ${
            selected === opt.value
              ? 'bg-prime text-prime-fg'
              : 'bg-page border border-wire text-dim hover:border-dim hover:text-ink'
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function SortArrow({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  const active = col === sortKey;
  return (
    <span aria-hidden className={`ml-1 text-[9px] ${active ? 'text-prime' : 'opacity-30'}`}>
      {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
    </span>
  );
}

function BrokerBadge({ broker }: { broker: string }) {
  const color = BROKER_COLORS[broker] ?? '#888';
  return (
    <span
      className="inline-block mt-0.5 px-1.5 py-px rounded text-[9px] font-semibold tracking-wide"
      style={{ background: color + '22', color }}
    >
      {BROKER_LABELS[broker] ?? broker}
    </span>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function ForeignEquityTable({ holdings, totals, fxRates, showEntityCol, lastUpdated }: Props) {
  const [sortKey, setSortKey]           = useState<SortKey>('current_market_value');
  const [sortDir, setSortDir]           = useState<SortDir>('desc');
  const [search, setSearch]             = useState('');
  const [filterBroker, setFilterBroker] = useState<string | null>(null);
  const [filterEntity, setFilterEntity] = useState<string | null>(null);
  const [display, setDisplay]           = useState<Display>('native');

  // Currencies offered by the switcher: Native + INR + any with a known FX rate.
  const switchOptions: Display[] = ['native', 'INR', ...SWITCH_CCYS.filter(c => fxRates?.[c] != null)];

  // INR value → selected display currency.
  //  native  : divide by the holding's own snapshot fx_rate (back to USD/SGD/…).
  //  other   : divide by the latest INR-per-unit rate for that currency.
  function conv(inrVal: number | null | undefined, rowFx: number | undefined): number | null {
    if (inrVal == null) return null;
    if (display === 'native') return inrVal / (rowFx || 1);
    return inrVal / (fxRates?.[display] ?? 1);
  }
  function ccyFor(rowCcy: string | undefined): string {
    return display === 'native' ? (rowCcy || 'INR') : display;
  }
  // Cross-currency totals need a single currency; in Native mode fall back to USD.
  const totalsCcy = display === 'native' ? 'USD' : display;
  function convTotal(inrVal: number | null | undefined): number | null {
    if (inrVal == null) return null;
    return inrVal / (fxRates?.[totalsCcy] ?? 1);
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  const brokers     = [...new Set(holdings.map(h => h.broker))].sort();
  const entityNames = [...new Set(holdings.map(h => h.entity_name).filter(Boolean) as string[])].sort();

  const filtered = useMemo(() => {
    let rows = holdings.filter(h => {
      if (filterBroker && h.broker !== filterBroker) return false;
      if (filterEntity && h.entity_name !== filterEntity) return false;
      return true;
    });
    if (search) {
      const q = search.toLowerCase();
      rows = rows.filter(h =>
        h.symbol.toLowerCase().includes(q) ||
        (h.entity_name ?? '').toLowerCase().includes(q) ||
        (h.isin ?? '').toLowerCase().includes(q) ||
        (BROKER_LABELS[h.broker] ?? h.broker).toLowerCase().includes(q),
      );
    }
    return rows;
  }, [holdings, search, filterBroker, filterEntity]);

  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No foreign equity holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear after the first international broker sync.</p>
      </div>
    );
  }

  const rows     = sortRows(filtered, sortKey, sortDir);
  const colCount = 3 + (showEntityCol ? 1 : 0) + 9;

  const base = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap sticky top-0 z-10';
  // Render helper (not a component) so header cells share the sort closure without
  // tripping react-hooks/static-components.
  const th = (col: SortKey, label: string, right = true) => (
    <th scope="col"
      className={`${base} ${right ? 'text-right' : 'text-left'}`}
      aria-sort={sortKey === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button onClick={() => handleSort(col)} className={`inline-flex items-center hover:text-ink transition-colors ${right ? 'ml-auto' : ''}`}>
        {label}<SortArrow col={col} sortKey={sortKey} sortDir={sortDir} />
      </button>
    </th>
  );

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-4 flex-wrap">
          <h2 className="text-base font-semibold text-ink">
            Foreign Equity Holdings
            {lastUpdated && (
              <span className="ml-3 text-xs font-normal text-ghost">
                updated {new Date(lastUpdated).toLocaleString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })}
              </span>
            )}
          </h2>
          {/* Currency switcher */}
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] text-ghost font-medium shrink-0">Show in:</span>
            {switchOptions.map(opt => (
              <button
                key={opt}
                onClick={() => setDisplay(opt)}
                className={`px-2 py-0.5 rounded text-[10px] font-medium transition-colors ${
                  display === opt
                    ? 'bg-prime text-prime-fg'
                    : 'bg-page border border-wire text-dim hover:border-dim hover:text-ink'
                }`}
              >
                {opt === 'native' ? 'Native' : opt}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <div>
            <p className="text-xs text-ghost mb-0.5">Total Cost</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtMoney(convTotal(totals.total_cost), totalsCcy)}</p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Current Value</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtMoney(convTotal(totals.total_current_market_value), totalsCcy)}</p>
          </div>
          {totals.cash_balance != null && totals.cash_balance > 0 && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Cash Balance</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtMoney(convTotal(totals.cash_balance), totalsCcy)}</p>
            </div>
          )}
          {totals.total_pnl_inception != null && totals.total_pnl_inception !== 0 && (
            <div>
              <p className="text-xs text-ghost mb-0.5">P&amp;L (Inception)</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totals.total_pnl_inception >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totals.total_pnl_inception >= 0 ? '+' : ''}{fmtMoney(convTotal(totals.total_pnl_inception), totalsCcy)}
              </p>
            </div>
          )}
          <div>
            <p className="text-xs text-ghost mb-0.5">Holdings</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{holdings.length}</p>
          </div>
          {display === 'native' && (
            <div className="self-end">
              <p className="text-[10px] text-ghost">Totals shown in USD (rows in native currency)</p>
            </div>
          )}
        </div>
      </div>

      {/* Filters */}
      <div className="px-5 sm:px-6 py-3 border-b border-rule flex flex-wrap gap-x-6 gap-y-2 items-center">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search symbol, ISIN, broker…"
          className="w-full max-w-xs text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors"
        />
        <FilterPills label="Broker" options={brokers} labelMap={BROKER_LABELS} selected={filterBroker} onChange={setFilterBroker} />
        {showEntityCol && <FilterPills label="Entity" options={entityNames} selected={filterEntity} onChange={setFilterEntity} />}
      </div>

      {/* Table */}
      <DragScroll className="overflow-auto max-h-[75vh]" role="region" aria-label="Foreign equity holdings table" tabIndex={0}>
        <table className="w-full text-sm" style={{ minWidth: '1100px' }}>
          <thead>
            <tr>
              <th scope="col" className={`${base} text-right pl-5 sm:pl-6 w-8`}>#</th>
              {th('symbol', 'Stock', false)}
              {showEntityCol && th('entity_name', 'Entity', false)}
              {th('exchange', 'Exch', false)}
              {th('quantity', 'Qty')}
              {th('avg_cost', 'Avg Cost')}
              {th('cost', 'Cost')}
              {th('first_invested_date', 'Since')}
              {th('current_market_value', 'Cur Value')}
              {th('exposure_pct', 'Exp %')}
              {th('pnl_inception', 'P&L (Inc)')}
              {th('xirr_inception_pct', 'XIRR')}
              <th scope="col" className={`${base} text-left pr-5 sm:pr-6`}>Remarks</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                  No holdings match your search.
                </td>
              </tr>
            ) : rows.map((h, i) => {
              const rowCcy = ccyFor(h.currency);
              return (
                <tr key={h.id} className="border-t border-rule hover:bg-page transition-colors duration-100">
                  <td className="px-3 pl-5 sm:pl-6 py-3 text-right tabular-nums text-xs text-ghost align-top">{i + 1}</td>
                  <td className="px-3 py-3 align-top">
                    <p className="text-xs font-medium text-ink whitespace-nowrap">{h.symbol || h.isin}</p>
                    <div className="flex flex-wrap gap-0.5 mt-0.5"><BrokerBadge broker={h.broker} /></div>
                    {h.isin && <p className="text-[10px] text-ghost font-mono mt-0.5">{h.isin}</p>}
                  </td>
                  {showEntityCol && (
                    <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.entity_name ?? '—'}</td>
                  )}
                  <td className="px-3 py-3 text-xs text-ghost whitespace-nowrap align-top">{h.exchange ?? '—'}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{h.quantity.toFixed(2)}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtMoney(conv(h.avg_cost, h.fx_rate), rowCcy, 2)}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtMoney(conv(h.cost, h.fx_rate), rowCcy)}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs align-top whitespace-nowrap">{fmtDate(h.first_invested_date)}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtMoney(conv(h.current_market_value, h.fx_rate), rowCcy)}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                    {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
                  </td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                    {h.pnl_inception == null
                      ? <span className="text-ghost">—</span>
                      : <span style={{ color: h.pnl_inception >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                          {h.pnl_inception >= 0 ? '+' : ''}{fmtMoney(conv(h.pnl_inception, h.fx_rate), rowCcy)}
                        </span>}
                  </td>
                  <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                    {h.xirr_inception_pct != null
                      ? <span style={{ color: h.xirr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.xirr_inception_pct)} p.a.</span>
                      : h.returns_inception_pct != null
                        ? <span style={{ color: h.returns_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.returns_inception_pct)} <span className="text-ghost">abs</span></span>
                        : <span className="text-ghost">—</span>}
                  </td>
                  <td className="px-3 pr-5 sm:pr-6 py-3 text-xs text-ghost align-top max-w-[160px]">{h.remarks ?? '—'}</td>
                </tr>
              );
            })}

            {rows.length > 0 && (
              <tr className="border-t-2 border-rule bg-page">
                <td colSpan={3 + (showEntityCol ? 1 : 0)} className="px-5 sm:px-6 py-3 text-xs font-semibold text-dim">
                  Total ({rows.length} holdings) · {totalsCcy}
                </td>
                <td />
                <td />
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtMoney(convTotal(rows.reduce((s, h) => s + (h.cost ?? 0), 0)), totalsCcy)}
                </td>
                <td />
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtMoney(convTotal(rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0)), totalsCcy)}
                </td>
                <td />
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {(() => {
                    const v = rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
                    return <span style={{ color: v >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{v >= 0 ? '+' : ''}{fmtMoney(convTotal(v), totalsCcy)}</span>;
                  })()}
                </td>
                <td />
                <td />
              </tr>
            )}
          </tbody>
        </table>
      </DragScroll>
    </div>
  );
}
