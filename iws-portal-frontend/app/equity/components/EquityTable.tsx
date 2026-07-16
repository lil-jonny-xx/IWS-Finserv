'use client';
import { useState, Fragment, useMemo } from 'react';
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
  prev_week_value?: number;
  market_value_as_on?: number;
  as_of_date?: string;
  exposure_pct?: number;
  weekly_change?: number;
  pnl_ytd?: number;
  pnl_inception?: number;
  pnl_weekly_change?: number;
  returns_ytd_pct?: number;
  returns_inception_pct?: number;
  cagr_inception_pct?: number;
  first_invested_date?: string;
  remarks?: string;
  brokers?: string[];  // set only in Combined view — all brokers holding this symbol
}

export interface EquityTotals {
  total_cost?: number;
  total_current_market_value?: number;
  total_pnl_inception?: number;
  total_pnl_ytd?: number;
  total_weekly_change?: number;
  grand_total?: number;
  cash_balance?: number;
  value_plus_cash?: number;
  portfolio_xirr_pct?: number | null;   // money-weighted return from ledger cash flows (entity scope)
  portfolio_income?: number | null;     // dividends + interest received (INR)
  portfolio_coverage?: string | null;   // 'full' | 'partial'
}

export interface CashBrokerRow {
  entity_id: number;
  entity_name: string;
  broker: string;
  balance: number;
  currency: string;
  balance_native?: number | null;
}

interface Props {
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
  cashByBroker?: CashBrokerRow[];
  showEntityCol: boolean;
}

const CASH_BROKER_LABEL: Record<string, string> = {
  zerodha: 'Zerodha', angel_one: 'Angel One', dhan: 'Dhan',
  ibkr: 'IBKR', vested: 'Vested', dbs: 'DBS', other: 'Other',
};

// Sum per-broker cash across the entities in scope → [{broker, total}], largest first.
function cashByBrokerTotals(rows: CashBrokerRow[]): { broker: string; total: number }[] {
  const m = new Map<string, number>();
  for (const r of rows) m.set(r.broker, (m.get(r.broker) ?? 0) + (r.balance || 0));
  return [...m.entries()]
    .map(([broker, total]) => ({ broker, total }))
    .filter(b => Math.abs(b.total) > 0.5)
    .sort((a, b) => b.total - a.total);
}

// ── formatters ────────────────────────────────────────────────────────────────

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  const abs = Math.round(Math.abs(n));
  const str = abs.toString();
  const last3 = str.slice(-3);
  const rest = str.slice(0, -3);
  const grouped = rest.length > 0
    ? rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',') + ',' + last3
    : last3;
  return (n < 0 ? '−₹' : '₹') + grouped;
}

function fmtPct(n: number | null | undefined): string {
  if (n == null) return '—';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

function fmtDate(iso: string | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function fmtDuration(iso: string | undefined): string {
  if (!iso) return '';
  const start = new Date(iso);
  const now = new Date();
  let years = now.getFullYear() - start.getFullYear();
  let months = now.getMonth() - start.getMonth();
  if (months < 0) { years--; months += 12; }
  if (years === 0 && months === 0) return '< 1m';
  if (years === 0) return `${months}m`;
  if (months === 0) return `${years}y`;
  return `${years}y ${months}m`;
}

function ColorNum({ n, fmt }: { n: number | null | undefined; fmt: (n: number) => string }) {
  if (n == null) return <span className="text-ghost">—</span>;
  return (
    <span style={{ color: n >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
      {fmt(n)}
    </span>
  );
}

// ── constants ─────────────────────────────────────────────────────────────────

const BROKER_LABELS: Record<string, string> = {
  zerodha:   'Zerodha',
  angel_one: 'Angel One',
  dhan:      'Dhan',
  ibkr:      'Interactive Brokers',
  vested:    'Vested',
  dbs:       'DBS Wealth',
  combined:  'Combined',
};

const BROKER_COLORS: Record<string, string> = {
  zerodha:   '#3772ff',
  angel_one: '#e05c00',
  dhan:      '#059669',
  ibkr:      '#d2122e',
  vested:    '#7c3aed',
  dbs:       '#b8860b',
};

// Native-currency symbols for international holdings (display only — all totals
// and aggregation use the INR-converted values).
const CCY_SYMBOL: Record<string, string> = { USD: '$', SGD: 'S$', GBP: '£', AED: 'AED ', HKD: 'HK$' };

function fmtNative(n: number | null | undefined, ccy: string | undefined): string {
  if (n == null || !ccy || ccy === 'INR') return '';
  const sym = CCY_SYMBOL[ccy] ?? (ccy + ' ');
  return sym + Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 2 });
}

// Sector display name, sort order, accent color
const SECTOR_META: Record<string, { label: string; order: number; accent: string }> = {
  'Equity':               { label: 'Equities',            order: 0, accent: 'var(--prime)' },
  'ETF':                  { label: 'ETF',                  order: 1, accent: '#6366f1' },
  'Gold ETF':             { label: 'Gold ETF',             order: 2, accent: '#d97706' },
  'Silver ETF':           { label: 'Silver ETF',           order: 3, accent: '#64748b' },
  'Sovereign Gold Bond':  { label: 'Sovereign Gold Bond',  order: 4, accent: '#b45309' },
};

function sectorMeta(sector: string | undefined | null) {
  return SECTOR_META[sector ?? 'Equity'] ?? { label: sector ?? 'Other', order: 99, accent: 'var(--prime)' };
}

type SortKey = keyof EquityHoldingRow;
type SortDir = 'asc' | 'desc';

// ── weighted avg CAGR ─────────────────────────────────────────────────────────

function weightedAvgCagr(rows: EquityHoldingRow[]): number | null {
  let sumW = 0, sumWC = 0;
  for (const h of rows) {
    if (h.cagr_inception_pct == null) continue;
    const w = h.current_market_value ?? 0;
    sumWC += h.cagr_inception_pct * w;
    sumW  += w;
  }
  return sumW > 0 ? sumWC / sumW : null;
}

// Value-weighted average of any percentage column (weighted by current market
// value) — used for the footer's Returns %/CAGR totals, where a plain sum is
// meaningless. Rows missing the metric are skipped (they don't dilute the mean).
function weightedAvgBy(
  rows: EquityHoldingRow[],
  key: 'returns_ytd_pct' | 'returns_inception_pct' | 'cagr_inception_pct',
): number | null {
  let sumW = 0, sumWV = 0;
  for (const h of rows) {
    const v = h[key];
    if (v == null) continue;
    const w = h.current_market_value ?? 0;
    sumWV += v * w;
    sumW  += w;
  }
  return sumW > 0 ? sumWV / sumW : null;
}

// ── sort ──────────────────────────────────────────────────────────────────────

function sortRows(rows: EquityHoldingRow[], key: SortKey, dir: SortDir): EquityHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = (a[key] as number | string) ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = (b[key] as number | string) ?? (typeof b[key] === 'number' ? -Infinity : '');
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

// ── combined view: merge same symbol across brokers (per entity) ──────────────

function mergeBySymbol(holdings: EquityHoldingRow[]): EquityHoldingRow[] {
  const map = new Map<string, EquityHoldingRow[]>();
  for (const h of holdings) {
    const key = `${h.entity_id}::${h.isin || h.symbol}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(h);
  }

  const merged: EquityHoldingRow[] = [];
  let syntheticId = 0;

  for (const rows of map.values()) {
    if (rows.length === 1) {
      merged.push({ ...rows[0], brokers: [rows[0].broker] });
      continue;
    }

    const qty  = rows.reduce((s, h) => s + h.quantity, 0);
    const cost = rows.reduce((s, h) => s + h.cost, 0);

    const hasCmv      = rows.some(h => h.current_market_value != null);
    const cmv         = hasCmv ? rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0) : undefined;
    const prevWeek    = rows.some(h => h.prev_week_value != null)
                        ? rows.reduce((s, h) => s + (h.prev_week_value ?? 0), 0) : undefined;
    const weeklyChg   = rows.some(h => h.weekly_change != null)
                        ? rows.reduce((s, h) => s + (h.weekly_change ?? 0), 0) : undefined;
    const pnlInc      = rows.some(h => h.pnl_inception != null)
                        ? rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0) : undefined;
    const pnlYtd      = rows.some(h => h.pnl_ytd != null)
                        ? rows.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0) : undefined;
    const pnlWeekly   = rows.some(h => h.pnl_weekly_change != null)
                        ? rows.reduce((s, h) => s + (h.pnl_weekly_change ?? 0), 0) : undefined;

    const returnsInc  = cost > 0 && pnlInc != null ? (pnlInc / cost) * 100 : undefined;
    const returnsYtd  = cost > 0 && pnlYtd != null ? (pnlYtd / cost) * 100 : undefined;

    const dates = rows.map(h => h.first_invested_date).filter(Boolean) as string[];
    const firstDate = dates.length > 0 ? [...dates].sort()[0] : undefined;
    let cagrInc: number | undefined;
    if (firstDate && cost > 0 && cmv != null && cmv > 0) {
      const years = (Date.now() - new Date(firstDate).getTime()) / (365.25 * 24 * 3600 * 1000);
      if (years >= 1.0) {
        try { cagrInc = (Math.pow(cmv / cost, 1 / years) - 1) * 100; } catch { /* */ }
      }
    }

    const brokers = [...new Set(rows.map(h => h.broker))];

    const displaySymbol = rows.find(r => r.symbol)?.symbol ?? rows[0].isin ?? '';
    merged.push({
      ...rows[0],
      id: -(++syntheticId),
      symbol: displaySymbol,
      broker: rows[0].broker,
      brokers,
      quantity: qty,
      avg_cost: qty > 0 ? cost / qty : 0,
      cost,
      current_price: rows.find(h => h.current_price != null)?.current_price,
      current_market_value: cmv,
      prev_week_value: prevWeek,
      weekly_change: weeklyChg,
      pnl_inception: pnlInc,
      pnl_ytd: pnlYtd,
      pnl_weekly_change: pnlWeekly,
      returns_inception_pct: returnsInc,
      returns_ytd_pct: returnsYtd,
      cagr_inception_pct: cagrInc,
      first_invested_date: firstDate,
      exposure_pct: undefined,
    });
  }

  // Recalculate exposure_pct against merged entity totals
  const entityTotals = new Map<number, number>();
  for (const h of merged) entityTotals.set(h.entity_id, (entityTotals.get(h.entity_id) ?? 0) + (h.current_market_value ?? 0));
  for (const h of merged) {
    const tot = entityTotals.get(h.entity_id) ?? 0;
    h.exposure_pct = tot > 0 && h.current_market_value != null
      ? Math.round(h.current_market_value / tot * 10000) / 100 : undefined;
  }

  return merged;
}

// ── filter pills ──────────────────────────────────────────────────────────────

function FilterPills({
  label, options, labelMap, selected, onChange,
}: {
  label: string;
  options: string[];
  labelMap?: Record<string, string>;
  selected: string | null;
  onChange: (v: string | null) => void;
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

// ── sort arrow ────────────────────────────────────────────────────────────────

function SortArrow({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  const active = col === sortKey;
  return (
    <span aria-hidden className={`ml-1 text-[9px] ${active ? 'text-prime' : 'opacity-30'}`}>
      {active ? (sortDir === 'asc' ? '↑' : '↓') : '↕'}
    </span>
  );
}

// ── section header (by sector) ────────────────────────────────────────────────

function SectionHeader({ sector, rows, colCount }: { sector: string; rows: EquityHoldingRow[]; colCount: number }) {
  const meta    = sectorMeta(sector);
  const cost    = rows.reduce((s, h) => s + h.cost, 0);
  const value   = rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0);
  const pnl     = rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const avgCagr = weightedAvgCagr(rows);
  const hasPnl  = rows.some(h => h.pnl_inception != null);

  return (
    <tr>
      <td colSpan={colCount} className="px-4 pl-5 sm:pl-6 py-2.5 bg-page border-t border-rule sticky left-0">
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="text-xs font-semibold" style={{ color: meta.accent }}>{meta.label}</span>
          <span className="text-[11px] text-ghost">Cost {fmtINR(cost)}</span>
          <span className="text-[11px] text-ghost">Cur Value {fmtINR(value)}</span>
          {hasPnl && (
            <span className="text-[11px] font-medium" style={{ color: pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              P&amp;L {pnl >= 0 ? '+' : ''}{fmtINR(pnl)}
            </span>
          )}
          {avgCagr != null && (
            <span className="text-[11px] font-medium" style={{ color: avgCagr >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              Avg CAGR {fmtPct(avgCagr)} p.a.
            </span>
          )}
          <span className="text-[11px] text-ghost">{rows.length} holding{rows.length !== 1 ? 's' : ''}</span>
        </div>
      </td>
    </tr>
  );
}

// ── table headers ─────────────────────────────────────────────────────────────

function TableHead({
  showEntityCol, sortKey, sortDir, onSort,
}: {
  showEntityCol: boolean;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
}) {
  const base = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap sticky top-0 z-10';

  function Th({ col, label, right = true, rowSpan = 1, borderL = false, first = false, className = '' }: {
    col: SortKey; label: string; right?: boolean; rowSpan?: number; borderL?: boolean; first?: boolean; className?: string;
  }) {
    return (
      <th scope="col" rowSpan={rowSpan}
        className={`${base} ${right ? 'text-right' : 'text-left'} ${borderL ? 'border-l border-rule' : ''} ${first ? 'pl-5 sm:pl-6' : ''} ${className}`}
        aria-sort={sortKey === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
      >
        <button onClick={() => onSort(col)} className={`inline-flex items-center hover:text-ink transition-colors ${right ? 'ml-auto' : ''}`}>
          {label}<SortArrow col={col} sortKey={sortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }

  function StaticTh({ label, colSpan = 1, borderL = false }: { label: string; colSpan?: number; borderL?: boolean }) {
    return (
      <th scope="col" colSpan={colSpan} className={`${base} text-center ${borderL ? 'border-l border-rule' : ''}`}>
        {label}
      </th>
    );
  }

  return (
    <thead>
      <tr>
        <th scope="col" rowSpan={2} className={`${base} text-right pl-5 sm:pl-6 w-8`}>#</th>
        <Th col="symbol"              label="Stock"     right={false} rowSpan={2} className="sticky left-0 z-20 bg-card" />
        {showEntityCol && <Th col="entity_name" label="Entity"  right={false} rowSpan={2} />}
        <Th col="exchange"            label="Exch"      right={false} rowSpan={2} />
        <Th col="quantity"            label="Qty"                     rowSpan={2} />
        <Th col="avg_cost"            label="Avg Cost"                rowSpan={2} />
        <Th col="cost"                label="Cost"                    rowSpan={2} />
        <Th col="first_invested_date" label="Since"                   rowSpan={2} />
        <Th col="current_market_value" label="Cur Value"              rowSpan={2} />
        <Th col="prev_week_value"     label="Prev Week"               rowSpan={2} />
        <Th col="weekly_change"       label="Wkly Chg"                rowSpan={2} />
        <Th col="exposure_pct"        label="Exp %"                   rowSpan={2} />
        <StaticTh label="P&L" colSpan={3} borderL />
        <StaticTh label="Returns" colSpan={3} borderL />
        <th scope="col" rowSpan={2} className={`${base} text-left pr-5 sm:pr-6`}>Remarks</th>
      </tr>
      <tr>
        <Th col="pnl_ytd"              label="YTD"      borderL />
        <Th col="pnl_inception"        label="Inception" />
        <Th col="pnl_weekly_change"    label="Wkly Chg" />
        <Th col="returns_ytd_pct"      label="YTD %"    borderL />
        <Th col="returns_inception_pct" label="Inc %" />
        <Th col="cagr_inception_pct"   label="CAGR" />
      </tr>
    </thead>
  );
}

// ── broker badge ──────────────────────────────────────────────────────────────

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

export default function EquityTable({ holdings, totals, cashByBroker = [], showEntityCol }: Props) {
  const cashBrokers = cashByBrokerTotals(cashByBroker);
  const [sortKey, setSortKey]         = useState<SortKey>('current_market_value');
  const [sortDir, setSortDir]         = useState<SortDir>('desc');
  const [search, setSearch]           = useState('');
  const [filterBroker, setFilterBroker]   = useState<string | null>(null);
  const [filterSector, setFilterSector]   = useState<string | null>(null);
  // Entity is filtered by the shared EntitySwitcher at the top of the page, so no
  // per-table entity filter here (it was redundant).

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  const brokers       = [...new Set(holdings.map(h => h.broker))].sort();
  const brokerOptions = brokers.length >= 2 ? [...brokers, 'combined'] : brokers;
  const sectors       = [...new Set(holdings.map(h => h.sector ?? 'Equity'))]
    .sort((a, b) => (sectorMeta(a).order - sectorMeta(b).order));

  const filtered = useMemo(() => {
    // Step 1: broker/sector filters (entity subset is scoped server-side).
    let rows = holdings.filter(h => {
      if (filterBroker && filterBroker !== 'combined' && h.broker !== filterBroker) return false;
      if (filterSector && (h.sector ?? 'Equity') !== filterSector) return false;
      return true;
    });

    // Step 2: merge by symbol+entity when Combined is selected
    if (filterBroker === 'combined') rows = mergeBySymbol(rows);

    // Step 3: text search (runs on merged rows so broker badges are matched correctly)
    if (search) {
      const q = search.toLowerCase();
      rows = rows.filter(h => {
        if (h.symbol.toLowerCase().includes(q)) return true;
        if ((h.entity_name ?? '').toLowerCase().includes(q)) return true;
        if ((h.isin ?? '').toLowerCase().includes(q)) return true;
        return (h.brokers ?? [h.broker]).some(b => BROKER_LABELS[b]?.toLowerCase().includes(q));
      });
    }

    return rows;
  }, [holdings, search, filterBroker, filterSector]);

  // Empty state — placed AFTER all hooks (useState×6 + useMemo) so the hook
  // call order is identical whether holdings are empty or populated, satisfying
  // React's Rules of Hooks (an early return above useMemo crashes on transition).
  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No equity holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear after the first broker sync.</p>
      </div>
    );
  }

  const rows = sortRows(filtered, sortKey, sortDir);

  // Group by sector, in defined order
  const activeSectors = [...new Set(rows.map(h => h.sector ?? 'Equity'))]
    .sort((a, b) => sectorMeta(a).order - sectorMeta(b).order);
  const bySector: Record<string, EquityHoldingRow[]> = {};
  for (const s of activeSectors) bySector[s] = rows.filter(h => (h.sector ?? 'Equity') === s);

  // Summary totals track the active client-side filters (broker/sector/search) so
  // the top strip and the bottom Total row always agree. 'combined' is a view, not
  // a filter, so it doesn't count as filtered (its merged sums equal the API totals).
  // A multi-entity subset (>1) means the page fetched ALL entities and we're
  // sub-selecting client-side, so totals must be recomputed. A single entity is
  // server-scoped (API totals already correct), so it isn't "filtered".
  const isFiltered  = !!((filterBroker && filterBroker !== 'combined') || filterSector || search);
  const view        = filtered;
  const asOfDate    = holdings.find(h => h.as_of_date)?.as_of_date;
  const avgCagrAll  = weightedAvgCagr(view);
  const totalPnlInc = view.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const totalPnlYtd = view.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0);
  const totalWeekly = view.reduce((s, h) => s + (h.weekly_change ?? 0), 0);
  const hasPnl      = view.some(h => h.pnl_inception != null);
  // Footer column totals ("total everything down"): value columns summed,
  // percentage columns value-weighted, exposure summed (→ ~100% of the view).
  const totalQty     = view.reduce((s, h) => s + h.quantity, 0);
  const totalPrevWk  = view.reduce((s, h) => s + (h.prev_week_value ?? 0), 0);
  const totalExp     = view.reduce((s, h) => s + (h.exposure_pct ?? 0), 0);
  const hasExp       = view.some(h => h.exposure_pct != null);
  const avgRetYtd    = weightedAvgBy(view, 'returns_ytd_pct');
  const avgRetInc    = weightedAvgBy(view, 'returns_inception_pct');
  const viewCost    = isFiltered ? view.reduce((s, h) => s + h.cost, 0) : totals.total_cost;
  const viewValue   = isFiltered ? view.reduce((s, h) => s + (h.current_market_value ?? 0), 0)
                                 : totals.total_current_market_value;
  const viewCount   = isFiltered ? view.length : holdings.length;

  // col count: # + symbol + [entity] + exch + qty + avg_cost + cost + since + mkt_val + prev_wk + wkly_chg + exp% + pnl×3 + ret×3 + remarks
  //          = 2 + [entity] + 16
  const colCount = 2 + (showEntityCol ? 1 : 0) + 16;

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-4">
          <h2 className="text-base font-semibold text-ink">
            Equity Holdings
            {asOfDate && <span className="ml-3 text-xs font-normal text-ghost">as of {fmtDate(asOfDate)}</span>}
          </h2>
        </div>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          <div>
            <p className="text-xs text-ghost mb-0.5">Total Cost</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(viewCost)}</p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Current Value</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(viewValue)}</p>
          </div>
          {!isFiltered && totals.cash_balance != null && totals.cash_balance > 0 && (
            <>
              <div>
                <p className="text-xs text-ghost mb-0.5">Cash Balance</p>
                <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.cash_balance)}</p>
              </div>
              <div>
                <p className="text-xs text-ghost mb-0.5">Value + Cash</p>
                <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.value_plus_cash)}</p>
              </div>
            </>
          )}
          {hasPnl && (
            <div>
              <p className="text-xs text-ghost mb-0.5">P&amp;L (Inception)</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totalPnlInc >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totalPnlInc >= 0 ? '+' : ''}{fmtINR(totalPnlInc)}
              </p>
            </div>
          )}
          {totalPnlYtd !== 0 && (
            <div>
              <p className="text-xs text-ghost mb-0.5">P&amp;L YTD</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totalPnlYtd >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totalPnlYtd >= 0 ? '+' : ''}{fmtINR(totalPnlYtd)}
              </p>
            </div>
          )}
          {totalWeekly !== 0 && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Weekly Chg</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totalWeekly >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {totalWeekly >= 0 ? '+' : ''}{fmtINR(totalWeekly)}
              </p>
            </div>
          )}
          {avgCagrAll != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Avg CAGR</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: avgCagrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {fmtPct(avgCagrAll)} p.a.
              </p>
            </div>
          )}
          {!isFiltered && totals.portfolio_xirr_pct != null && (
            <div title="Money-weighted return (XIRR) from actual deposits, withdrawals & dividends in the broker ledger">
              <p className="text-xs text-ghost mb-0.5">Portfolio XIRR</p>
              <p className="text-sm font-semibold tabular-nums"
                style={{ color: totals.portfolio_xirr_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {fmtPct(totals.portfolio_xirr_pct)} p.a.
              </p>
            </div>
          )}
          {!isFiltered && totals.portfolio_income != null && totals.portfolio_income > 0 && (
            <div title="Dividends & interest received (from broker ledgers)">
              <p className="text-xs text-ghost mb-0.5">Dividends/Int</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.portfolio_income)}</p>
            </div>
          )}
          <div>
            <p className="text-xs text-ghost mb-0.5">Holdings</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{viewCount}</p>
          </div>
        </div>

        {/* Cash held by each Indian broker, for the entities in scope. */}
        {!isFiltered && cashBrokers.length > 0 && (
          <div className="mt-3 pt-3 border-t border-rule">
            <p className="text-[11px] uppercase tracking-wide text-ghost mb-1.5">Cash by broker</p>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              {cashBrokers.map(b => (
                <span key={b.broker} className="text-xs text-dim">
                  {CASH_BROKER_LABEL[b.broker] ?? b.broker}
                  <span className="ml-1.5 font-semibold text-ink tabular-nums">{fmtINR(b.total)}</span>
                </span>
              ))}
            </div>
          </div>
        )}
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
        <FilterPills
          label="Sector"
          options={sectors}
          labelMap={Object.fromEntries(Object.entries(SECTOR_META).map(([k, v]) => [k, v.label]))}
          selected={filterSector}
          onChange={setFilterSector}
        />
        <FilterPills label="Broker" options={brokerOptions} labelMap={BROKER_LABELS} selected={filterBroker} onChange={setFilterBroker} />
      </div>

      {/* Table */}
      <DragScroll className="overflow-auto max-h-[75vh]" role="region" aria-label="Equity holdings table" tabIndex={0}>
        <table className="w-full text-sm" style={{ minWidth: '1400px' }}>
          <TableHead showEntityCol={showEntityCol} sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                  No holdings match your search.
                </td>
              </tr>
            ) : (
              activeSectors.map(sector => {
                const group = bySector[sector];
                if (!group?.length) return null;
                return (
                  <Fragment key={sector}>
                    <SectionHeader sector={sector} rows={group} colCount={colCount} />
                    {group.map((h, i) => (
                      <tr key={h.id} className="border-t border-rule hover:bg-page transition-colors duration-100">
                        <td className="px-3 pl-5 sm:pl-6 py-3 text-right tabular-nums text-xs text-ghost align-top">{i + 1}</td>
                        <td className="px-3 py-3 align-top sticky left-0 bg-card hover:bg-page">
                          <p className="text-xs font-medium text-ink whitespace-nowrap">{h.symbol || h.isin}</p>
                          <div className="flex flex-wrap gap-0.5 mt-0.5">
                            {(h.brokers ?? [h.broker]).map(b => <BrokerBadge key={b} broker={b} />)}
                          </div>
                          {h.isin && <p className="text-[10px] text-ghost font-mono mt-0.5">{h.isin}</p>}
                        </td>
                        {showEntityCol && (
                          <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.entity_name ?? '—'}</td>
                        )}
                        <td className="px-3 py-3 text-xs text-ghost whitespace-nowrap align-top">{h.exchange ?? '—'}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{h.quantity.toFixed(0)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {fmtINR(h.avg_cost)}
                          {h.currency && h.currency !== 'INR' && h.avg_cost_native != null && (
                            <p className="text-[10px] text-ghost mt-0.5">{fmtNative(h.avg_cost_native, h.currency)}</p>
                          )}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.cost)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs align-top whitespace-nowrap">
                          <span className="text-ink">{fmtDate(h.first_invested_date)}</span>
                          {h.first_invested_date && <p className="text-[10px] text-ghost mt-0.5">{fmtDuration(h.first_invested_date)}</p>}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">
                          {fmtINR(h.current_market_value)}
                          {h.currency && h.currency !== 'INR' && h.current_market_value_native != null && (
                            <p className="text-[10px] text-ghost mt-0.5">{fmtNative(h.current_market_value_native, h.currency)}</p>
                          )}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(h.prev_week_value)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.weekly_change} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.pnl_ytd} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_inception} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_weekly_change} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.returns_ytd_pct} fmt={fmtPct} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.returns_inception_pct} fmt={fmtPct} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          {h.cagr_inception_pct != null
                            ? <span style={{ color: h.cagr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.cagr_inception_pct)} p.a.</span>
                            : h.returns_inception_pct != null
                              ? <span style={{ color: h.returns_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.returns_inception_pct)} <span className="text-ghost">abs</span></span>
                              : <span className="text-ghost">—</span>}
                        </td>
                        <td className="px-3 pr-5 sm:pr-6 py-3 text-xs text-ghost align-top max-w-[160px]">{h.remarks ?? '—'}</td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })
            )}

            {/* Overall totals footer — every column totalled: value columns summed,
                percentage columns value-weighted, exposure summed. */}
            {rows.length > 0 && (
              <tr className="border-t-2 border-rule bg-page">
                {/* label spans #, symbol, [entity], exchange (4 + entity) so Qty gets
                    its own cell and each total lands under its column. */}
                <td colSpan={4 + (showEntityCol ? 1 : 0)} className="px-5 sm:px-6 py-3 text-xs font-semibold text-dim">
                  Total ({rows.length} holdings)
                </td>
                {/* Qty */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {totalQty.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </td>
                {/* Avg Cost — a per-share price, not summable */}
                <td className="px-3 py-3 text-right tabular-nums text-xs text-ghost whitespace-nowrap">—</td>
                {/* Cost */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + h.cost, 0))}
                </td>
                {/* Since */}
                <td />
                {/* Cur Value */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0))}
                </td>
                {/* Prev Week */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-dim whitespace-nowrap">
                  {totalPrevWk ? fmtINR(totalPrevWk) : '—'}
                </td>
                {/* Wkly Chg */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={totalWeekly || null} fmt={fmtINR} />
                </td>
                {/* Exp % */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-dim whitespace-nowrap">
                  {hasExp ? totalExp.toFixed(2) + '%' : '—'}
                </td>
                {/* P&L YTD / Inception / Wkly */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap border-l border-rule">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_weekly_change ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                {/* Returns YTD % / Inc % (value-weighted) */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap border-l border-rule">
                  {avgRetYtd != null
                    ? <span style={{ color: avgRetYtd >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgRetYtd)}</span>
                    : <span className="text-ghost">—</span>}
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {avgRetInc != null
                    ? <span style={{ color: avgRetInc >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgRetInc)}</span>
                    : <span className="text-ghost">—</span>}
                </td>
                {/* CAGR (value-weighted) */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {avgCagrAll != null
                    ? <span style={{ color: avgCagrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgCagrAll)} p.a.</span>
                    : <span className="text-ghost">—</span>}
                </td>
                {/* Remarks */}
                <td />
              </tr>
            )}
          </tbody>
        </table>
      </DragScroll>
    </div>
  );
}
