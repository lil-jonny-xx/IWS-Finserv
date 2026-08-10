'use client';
import { useState, Fragment, useMemo } from 'react';
import DragScroll from '@/app/components/DragScroll';

// Row shape is the shared _row_to_holding payload (main.py) — the same one the
// Equity and Foreign Equity tables consume. Commodities come from BOTH
// equity_holding and foreign_equity_holding (the API UNIONs them), so every row
// may carry native-currency figures.
export interface CommodityHoldingRow {
  id: number;
  entity_id: number;
  entity_name?: string | null;
  broker: string;
  symbol: string;
  isin?: string | null;
  exchange?: string | null;
  sector?: string | null;
  asset_class: string;
  quantity: number | null;
  avg_cost: number | null;
  cost: number | null;
  current_price?: number | null;
  current_market_value: number | null;
  currency?: string;
  fx_rate?: number | null;
  avg_cost_native?: number | null;
  cost_native?: number | null;
  current_price_native?: number | null;
  current_market_value_native?: number | null;
  prev_week_value?: number | null;
  as_of_date?: string | null;
  exposure_pct?: number | null;
  weekly_change?: number | null;
  // Derived client-side (see withWeeklyPct) — the leading Wkly Chg column shows the
  // percentage move, while the one under P&L keeps the rupee figure.
  weekly_change_pct?: number | null;
  pnl_ytd?: number | null;
  pnl_inception?: number | null;
  pnl_weekly_change?: number | null;
  returns_ytd_pct?: number | null;
  returns_inception_pct?: number | null;
  cagr_inception_pct?: number | null;
  xirr_inception_pct?: number | null;
  fy_returns?: FYReturns | null;
  first_invested_date?: string | null;
  remarks?: string | null;
  brokers?: string[];   // set only in the Combined view — every broker holding this symbol
}

// Growth for COMPLETED financial years, keyed "2025-26". The current FY is not in
// here — it stays in returns_ytd_pct. A year absent means it isn't knowable, which
// renders "—" rather than a zero.
export type FYReturns = Record<string, { pnl: number; pct: number; base: number }>;

export interface CommodityTotals {
  total_cost?: number;
  total_current_market_value?: number;
  total_prev_week_value?: number;
  total_weekly_change?: number;
  total_pnl_inception?: number;
  total_pnl_ytd?: number;
  total_pnl_weekly_change?: number;
}

interface Props {
  holdings: CommodityHoldingRow[];
  totals: CommodityTotals;
  showEntityCol: boolean;
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

// Units can be fractional (SGB tranches, fractional ETF lots) but are usually whole —
// so show up to 4 decimals and drop the trailing zeros rather than pad every row.
function fmtQty(n: number | null | undefined): string {
  if (n == null) return '—';
  return n.toLocaleString('en-IN', { maximumFractionDigits: 4 });
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

function fmtDuration(iso: string | null | undefined): string {
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

const CCY_SYMBOL: Record<string, string> = {
  USD: '$', SGD: 'S$', GBP: '£', EUR: '€', AED: 'AED ', HKD: 'HK$', CHF: 'CHF ',
};

function fmtNative(n: number | null | undefined, ccy: string | null | undefined): string {
  if (n == null || !ccy || ccy === 'INR') return '';
  const sym = CCY_SYMBOL[ccy] ?? (ccy + ' ');
  return sym + Math.abs(n).toLocaleString('en-US', { maximumFractionDigits: 2 });
}

function ColorNum({ n, fmt }: { n: number | null | undefined; fmt: (n: number) => string }) {
  if (n == null) return <span className="text-ghost">—</span>;
  return <span style={{ color: n >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmt(n)}</span>;
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

// asset_class → display name + accent. Drives the "Type" filter row, which is the
// metals-vs-commodities split the page used to hardcode into two separate tables.
const CLASS_META: Record<string, { label: string; order: number; accent: string }> = {
  gold:      { label: 'Gold',      order: 0, accent: '#d97706' },
  silver:    { label: 'Silver',    order: 1, accent: '#64748b' },
  commodity: { label: 'Commodity', order: 2, accent: '#0891b2' },
};

function classMeta(cls: string | null | undefined) {
  return CLASS_META[cls ?? ''] ?? { label: cls ?? 'Other', order: 99, accent: 'var(--prime)' };
}

// Sector display name, sort order, accent — sections are grouped by sector, exactly
// as the Equity table does.
const SECTOR_META: Record<string, { label: string; order: number; accent: string }> = {
  'Gold ETF':            { label: 'Gold ETF',            order: 0, accent: '#d97706' },
  'Sovereign Gold Bond': { label: 'Sovereign Gold Bond', order: 1, accent: '#b45309' },
  'Silver ETF':          { label: 'Silver ETF',          order: 2, accent: '#64748b' },
  'Commodity':           { label: 'Commodity',           order: 3, accent: '#0891b2' },
};

function sectorMeta(sector: string | null | undefined) {
  return SECTOR_META[sector ?? ''] ?? { label: sector ?? 'Other', order: 99, accent: 'var(--prime)' };
}

type SortKey = keyof CommodityHoldingRow;
type SortDir = 'asc' | 'desc';

// ── FY helpers ────────────────────────────────────────────────────────────────

// FY labels present anywhere in the data, newest first. Driven by the rows rather
// than hardcoded, so the columns roll forward on their own each April.
function fyLabelsOf(rows: CommodityHoldingRow[]): string[] {
  const s = new Set<string>();
  for (const h of rows) for (const k of Object.keys(h.fy_returns ?? {})) s.add(k);
  return [...s].sort().reverse();
}

// Value-weighted FY % across rows: Σpnl / Σbase, NOT a mean of percentages — each
// row's return stands on its own capital base.
function fyTotal(rows: CommodityHoldingRow[], label: string): { pnl: number; pct: number | null } {
  let pnl = 0, base = 0, seen = false;
  for (const h of rows) {
    const v = h.fy_returns?.[label];
    if (!v) continue;
    seen = true;
    pnl  += v.pnl;
    base += v.base;
  }
  return { pnl: seen ? pnl : 0, pct: base > 0 ? (pnl / base) * 100 : null };
}

// ── weighted averages ─────────────────────────────────────────────────────────

// Value-weighted average of a percentage column (weighted by current market value).
// Rows missing the metric are skipped so they don't dilute the mean.
function weightedAvgBy(
  rows: CommodityHoldingRow[],
  key: 'returns_ytd_pct' | 'returns_inception_pct' | 'cagr_inception_pct' | 'xirr_inception_pct',
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

// Weekly move as a share of the prior week's value. The rupee figure was being
// shown twice (once beside Prev Week, once under P&L), so the leading column now
// carries the percentage and the P&L one keeps the money.
function withWeeklyPct(rows: CommodityHoldingRow[]): CommodityHoldingRow[] {
  return rows.map(h => ({
    ...h,
    weekly_change_pct: h.weekly_change != null && h.prev_week_value
      ? (h.weekly_change / h.prev_week_value) * 100
      : undefined,
  }));
}

// ── sort ──────────────────────────────────────────────────────────────────────

function sortRows(rows: CommodityHoldingRow[], key: SortKey, dir: SortDir): CommodityHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = (a[key] as number | string) ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = (b[key] as number | string) ?? (typeof b[key] === 'number' ? -Infinity : '');
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

// ── combined view: merge the same instrument across brokers (per entity) ───────

function mergeBySymbol(holdings: CommodityHoldingRow[]): CommodityHoldingRow[] {
  const map = new Map<string, CommodityHoldingRow[]>();
  for (const h of holdings) {
    // ISIN first so an Angel One "-EQ" suffixed ticker folds into the plain one.
    const key = `${h.entity_id}::${h.isin || h.symbol}`;
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(h);
  }

  const merged: CommodityHoldingRow[] = [];
  let syntheticId = 0;

  for (const rows of map.values()) {
    if (rows.length === 1) {
      merged.push({ ...rows[0], brokers: [rows[0].broker] });
      continue;
    }

    const sum = (k: keyof CommodityHoldingRow) =>
      rows.some(h => h[k] != null)
        ? rows.reduce((s, h) => s + ((h[k] as number | null) ?? 0), 0)
        : undefined;

    const qty  = sum('quantity') ?? 0;
    const cost = sum('cost') ?? 0;
    const cmv  = sum('current_market_value');
    const pnlInc = sum('pnl_inception');
    const pnlYtd = sum('pnl_ytd');

    const dates = rows.map(h => h.first_invested_date).filter(Boolean) as string[];
    const firstDate = dates.length > 0 ? [...dates].sort()[0] : undefined;

    // CAGR is only meaningful past a year of holding — same gate the workers apply.
    let cagrInc: number | undefined;
    if (firstDate && cost > 0 && cmv != null && cmv > 0) {
      const years = (Date.now() - new Date(firstDate).getTime()) / (365.25 * 24 * 3600 * 1000);
      if (years >= 1.0) cagrInc = (Math.pow(cmv / cost, 1 / years) - 1) * 100;
    }

    const sameCcy = rows.every(h => (h.currency ?? 'INR') === (rows[0].currency ?? 'INR'));
    const brokers = [...new Set(rows.map(h => h.broker))];

    merged.push({
      ...rows[0],
      id: -(++syntheticId),
      symbol: rows.find(r => r.symbol)?.symbol ?? rows[0].isin ?? '',
      broker: rows[0].broker,
      brokers,
      quantity: qty,
      avg_cost: qty > 0 ? cost / qty : 0,
      cost,
      current_price: rows.find(h => h.current_price != null)?.current_price,
      current_market_value: cmv ?? null,
      // Native figures only survive a merge when every leg is in the same currency.
      current_market_value_native: sameCcy ? sum('current_market_value_native') : undefined,
      avg_cost_native: sameCcy && qty > 0 ? ((sum('cost_native') ?? 0) / qty) : undefined,
      prev_week_value: sum('prev_week_value'),
      weekly_change: sum('weekly_change'),
      pnl_inception: pnlInc,
      pnl_ytd: pnlYtd,
      pnl_weekly_change: sum('pnl_weekly_change'),
      returns_inception_pct: cost > 0 && pnlInc != null ? (pnlInc / cost) * 100 : undefined,
      returns_ytd_pct: cost > 0 && pnlYtd != null ? (pnlYtd / cost) * 100 : undefined,
      cagr_inception_pct: cagrInc,
      // XIRR can't be re-derived without the cash flows, so value-weight the legs
      // that have one rather than inventing a number.
      xirr_inception_pct: weightedAvgBy(rows, 'xirr_inception_pct') ?? undefined,
      // Re-derive per FY from the merged rows: Σpnl / Σbase is the only correct way
      // to combine returns that each stand on their own capital.
      fy_returns: (() => {
        const out: FYReturns = {};
        for (const l of fyLabelsOf(rows)) {
          const t = fyTotal(rows, l);
          const base = rows.reduce((s, h) => s + (h.fy_returns?.[l]?.base ?? 0), 0);
          if (base > 0) out[l] = { pnl: t.pnl, pct: t.pct ?? 0, base };
        }
        return Object.keys(out).length ? out : null;
      })(),
      first_invested_date: firstDate,
      exposure_pct: undefined,
    });
  }

  // Recalculate exposure against the merged entity totals.
  const entityTotals = new Map<number, number>();
  for (const h of merged) {
    entityTotals.set(h.entity_id, (entityTotals.get(h.entity_id) ?? 0) + (h.current_market_value ?? 0));
  }
  for (const h of merged) {
    const tot = entityTotals.get(h.entity_id) ?? 0;
    h.exposure_pct = tot > 0 && h.current_market_value != null
      ? Math.round((h.current_market_value / tot) * 10000) / 100 : undefined;
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

function SectionHeader({ sector, rows, colCount }: {
  sector: string; rows: CommodityHoldingRow[]; colCount: number;
}) {
  const meta   = sectorMeta(sector);
  const cost   = rows.reduce((s, h) => s + (h.cost ?? 0), 0);
  const value  = rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0);
  const pnl    = rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const hasPnl = rows.some(h => h.pnl_inception != null);
  const avgCagr = weightedAvgBy(rows, 'cagr_inception_pct');

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
  showEntityCol, showNative, sortKey, sortDir, onSort, fyLabels,
}: {
  showEntityCol: boolean;
  showNative: boolean;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
  fyLabels: string[];
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
        <Th col="symbol"               label="Instrument" right={false} rowSpan={2} className="sticky left-0 z-20 bg-card" />
        <Th col="first_invested_date"  label="Bought on"  right={false} rowSpan={2} />
        {showEntityCol && <Th col="entity_name" label="Entity" right={false} rowSpan={2} />}
        <Th col="exchange"             label="Exch"       right={false} rowSpan={2} />
        <Th col="quantity"             label="Qty"                      rowSpan={2} />
        <Th col="avg_cost"             label="Avg Cost"                 rowSpan={2} />
        <Th col="cost"                 label="Cost"                     rowSpan={2} />
        {showNative && <Th col="current_market_value_native" label="Native Value" rowSpan={2} />}
        <Th col="current_market_value" label="Cur Value"                rowSpan={2} />
        <Th col="prev_week_value"      label="Prev Week"                rowSpan={2} />
        <Th col="weekly_change_pct"    label="Wkly Chg %"               rowSpan={2} />
        <Th col="exposure_pct"         label="Exp %"                    rowSpan={2} />
        <StaticTh label="P&L" colSpan={3} borderL />
        <StaticTh label="Returns" colSpan={4} borderL />
        {fyLabels.length > 0 && <StaticTh label="FY Growth" colSpan={fyLabels.length} borderL />}
        <th scope="col" rowSpan={2} className={`${base} text-left pr-5 sm:pr-6`}>Remarks</th>
      </tr>
      <tr>
        <Th col="pnl_ytd"               label="YTD"       borderL />
        <Th col="pnl_inception"         label="Inception" />
        <Th col="pnl_weekly_change"     label="Wkly Chg" />
        <Th col="returns_ytd_pct"       label="YTD %"     borderL />
        <Th col="returns_inception_pct" label="Inc %" />
        <Th col="cagr_inception_pct"    label="CAGR" />
        <Th col="xirr_inception_pct"    label="XIRR" />
        {/* Not sortable: each FY is its own key inside the JSON, and the sort
            machinery keys off flat row columns. */}
        {fyLabels.map((l, i) => (
          <StaticTh key={l} label={`FY${l}`} borderL={i === 0} />
        ))}
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

export default function CommodityTable({ holdings, totals, showEntityCol }: Props) {
  const [sortKey, setSortKey]           = useState<SortKey>('current_market_value');
  const [sortDir, setSortDir]           = useState<SortDir>('desc');
  const [search, setSearch]             = useState('');
  const [filterClass, setFilterClass]   = useState<string | null>(null);
  const [filterSector, setFilterSector] = useState<string | null>(null);
  const [filterBroker, setFilterBroker] = useState<string | null>(null);
  // Entity is filtered by the shared EntitySwitcher at the top of the page, so
  // there's no per-table entity filter here.

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => (d === 'asc' ? 'desc' : 'asc'));
    else { setSortKey(key); setSortDir('desc'); }
  }

  const classes = [...new Set(holdings.map(h => h.asset_class))]
    .sort((a, b) => classMeta(a).order - classMeta(b).order);
  const sectors = [...new Set(holdings.map(h => h.sector ?? 'Other'))]
    .sort((a, b) => sectorMeta(a).order - sectorMeta(b).order);
  const brokers = [...new Set(holdings.map(h => h.broker))].sort();
  const brokerOptions = brokers.length >= 2 ? [...brokers, 'combined'] : brokers;

  const filtered = useMemo(() => {
    // Step 1: class/sector/broker filters (the entity subset is scoped server-side).
    let rows = holdings.filter(h => {
      if (filterClass && h.asset_class !== filterClass) return false;
      if (filterSector && (h.sector ?? 'Other') !== filterSector) return false;
      if (filterBroker && filterBroker !== 'combined' && h.broker !== filterBroker) return false;
      return true;
    });

    // Step 2: merge by symbol+entity when Combined is selected.
    if (filterBroker === 'combined') rows = mergeBySymbol(rows);

    // Step 3: text search — runs on merged rows so broker badges match correctly.
    if (search) {
      const q = search.toLowerCase();
      rows = rows.filter(h => {
        if (h.symbol.toLowerCase().includes(q)) return true;
        if ((h.entity_name ?? '').toLowerCase().includes(q)) return true;
        if ((h.isin ?? '').toLowerCase().includes(q)) return true;
        if ((h.sector ?? '').toLowerCase().includes(q)) return true;
        return (h.brokers ?? [h.broker]).some(b => BROKER_LABELS[b]?.toLowerCase().includes(q));
      });
    }

    // Step 4: derive the weekly % (after merging, so a combined row's % comes from
    // its summed change over its summed prior-week value).
    return withWeeklyPct(rows);
  }, [holdings, search, filterClass, filterSector, filterBroker]);

  // FY columns are driven by ALL holdings, not the filtered view, so the columns
  // don't appear/disappear as you filter.
  const fyLabels = useMemo(() => fyLabelsOf(holdings), [holdings]);

  // Empty state — placed AFTER every hook so the hook call order is identical
  // whether holdings are empty or populated (React's Rules of Hooks).
  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No commodity holdings on record</p>
        <p className="text-xs text-ghost">
          Gold / silver ETFs, sovereign gold bonds and commodity instruments appear here after the next broker sync.
        </p>
      </div>
    );
  }

  const rows = sortRows(filtered, sortKey, sortDir);

  // Group by sector, in the defined order.
  const activeSectors = [...new Set(rows.map(h => h.sector ?? 'Other'))]
    .sort((a, b) => sectorMeta(a).order - sectorMeta(b).order);
  const bySector: Record<string, CommodityHoldingRow[]> = {};
  for (const s of activeSectors) bySector[s] = rows.filter(h => (h.sector ?? 'Other') === s);

  // Summary totals track the active client-side filters so the top strip and the
  // bottom Total row always agree. 'combined' is a view, not a filter, so it doesn't
  // count as filtered (its merged sums equal the API totals).
  const isFiltered = !!(filterClass || filterSector || (filterBroker && filterBroker !== 'combined') || search);
  const view       = filtered;
  const asOfDate   = holdings.find(h => h.as_of_date)?.as_of_date;
  const showNative = holdings.some(h => (h.currency ?? 'INR') !== 'INR');

  const viewCost  = isFiltered ? view.reduce((s, h) => s + (h.cost ?? 0), 0) : totals.total_cost;
  const viewValue = isFiltered ? view.reduce((s, h) => s + (h.current_market_value ?? 0), 0)
                               : totals.total_current_market_value;
  const viewCount = isFiltered ? view.length : holdings.length;

  const totalPnlInc = view.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const totalPnlYtd = view.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0);
  const totalWeekly = view.reduce((s, h) => s + (h.weekly_change ?? 0), 0);
  const hasPnl      = view.some(h => h.pnl_inception != null);

  // Footer column totals ("total everything down"): value columns summed,
  // percentage columns value-weighted, exposure summed (→ ~100% of the view).
  const totalQty    = view.reduce((s, h) => s + (h.quantity ?? 0), 0);
  const totalPrevWk = view.reduce((s, h) => s + (h.prev_week_value ?? 0), 0);
  const totalWeeklyPct = totalPrevWk ? (totalWeekly / totalPrevWk) * 100 : null;
  const totalExp    = view.reduce((s, h) => s + (h.exposure_pct ?? 0), 0);
  const hasExp      = view.some(h => h.exposure_pct != null);
  const avgRetYtd   = weightedAvgBy(view, 'returns_ytd_pct');
  const avgRetInc   = weightedAvgBy(view, 'returns_inception_pct');
  const avgCagrAll  = weightedAvgBy(view, 'cagr_inception_pct');
  const avgXirrAll  = weightedAvgBy(view, 'xirr_inception_pct');

  // col count: # + instrument + bought_on + [entity] + exch + qty + avg + cost
  //          + [native] + cur + prev + wkly% + exp + pnl×3 + returns×4 + remarks
  //          = 19 + [entity] + [native], plus one per completed FY
  const colCount = 19 + (showEntityCol ? 1 : 0) + (showNative ? 1 : 0) + fyLabels.length;

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-4">
          <h2 className="text-base font-semibold text-ink">
            Commodity Holdings
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
          {avgXirrAll != null && (
            <div title="Money-weighted return (XIRR) from the actual purchase/sale cash flows, value-weighted across holdings">
              <p className="text-xs text-ghost mb-0.5">Avg XIRR</p>
              <p className="text-sm font-semibold tabular-nums"
                 style={{ color: avgXirrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                {fmtPct(avgXirrAll)} p.a.
              </p>
            </div>
          )}
          <div>
            <p className="text-xs text-ghost mb-0.5">Holdings</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{viewCount}</p>
          </div>
        </div>
      </div>

      {/* Filters */}
      <div className="px-5 sm:px-6 py-3 border-b border-rule flex flex-wrap gap-x-6 gap-y-2 items-center">
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search instrument, ISIN, sector, broker…"
          className="w-full max-w-xs text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors"
        />
        <FilterPills
          label="Type"
          options={classes}
          labelMap={Object.fromEntries(Object.entries(CLASS_META).map(([k, v]) => [k, v.label]))}
          selected={filterClass}
          onChange={setFilterClass}
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
      <DragScroll className="overflow-auto max-h-[75vh]" role="region" aria-label="Commodity holdings table" tabIndex={0}>
        <table className="w-full text-sm" style={{ minWidth: '1400px' }}>
          <TableHead
            showEntityCol={showEntityCol}
            showNative={showNative}
            sortKey={sortKey}
            sortDir={sortDir}
            onSort={handleSort}
            fyLabels={fyLabels}
          />
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                  No holdings match your filters.
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
                        <td className="px-3 py-3 tabular-nums text-xs align-top whitespace-nowrap">
                          <span className="text-ink">{fmtDate(h.first_invested_date)}</span>
                          {h.first_invested_date && <p className="text-[10px] text-ghost mt-0.5">{fmtDuration(h.first_invested_date)}</p>}
                        </td>
                        {showEntityCol && (
                          <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.entity_name ?? '—'}</td>
                        )}
                        <td className="px-3 py-3 text-xs text-ghost whitespace-nowrap align-top">{h.exchange ?? '—'}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtQty(h.quantity)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {fmtINR(h.avg_cost)}
                          {h.currency && h.currency !== 'INR' && h.avg_cost_native != null && (
                            <p className="text-[10px] text-ghost mt-0.5">{fmtNative(h.avg_cost_native, h.currency)}</p>
                          )}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.cost)}</td>
                        {showNative && (
                          <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                            {(h.currency ?? 'INR') !== 'INR'
                              ? (fmtNative(h.current_market_value_native, h.currency) || '—')
                              : '—'}
                          </td>
                        )}
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.current_market_value)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(h.prev_week_value)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.weekly_change_pct} fmt={fmtPct} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
                          {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.pnl_ytd} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_inception} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_weekly_change} fmt={fmtINR} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.returns_ytd_pct} fmt={fmtPct} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.returns_inception_pct} fmt={fmtPct} /></td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          {/* Under a year held there is no CAGR — fall back to the absolute
                              return (clearly labelled) rather than showing nothing. */}
                          {h.cagr_inception_pct != null
                            ? <span style={{ color: h.cagr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.cagr_inception_pct)} p.a.</span>
                            : h.returns_inception_pct != null
                              ? <span style={{ color: h.returns_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.returns_inception_pct)} <span className="text-ghost">abs</span></span>
                              : <span className="text-ghost">—</span>}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
                          {h.xirr_inception_pct != null
                            ? <span style={{ color: h.xirr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.xirr_inception_pct)} p.a.</span>
                            : <span className="text-ghost">—</span>}
                        </td>
                        {fyLabels.map((l, i) => (
                          <td key={l}
                              className={`px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top ${i === 0 ? 'border-l border-rule' : ''}`}>
                            <ColorNum n={h.fy_returns?.[l]?.pct ?? null} fmt={fmtPct} />
                          </td>
                        ))}
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
                {/* Label spans exactly the non-numeric lead-in: #, Instrument, Bought on,
                    Exch — four columns, plus Entity when shown. One too many here would
                    push every total a column right of its heading. */}
                <td colSpan={4 + (showEntityCol ? 1 : 0)} className="px-5 sm:px-6 py-3 text-xs font-semibold text-dim">
                  Total ({rows.length} holdings)
                </td>
                {/* Qty — units of different instruments, summed for completeness */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">{fmtQty(totalQty)}</td>
                {/* Avg Cost — a per-unit price, not summable */}
                <td className="px-3 py-3 text-right tabular-nums text-xs text-ghost whitespace-nowrap">—</td>
                {/* Cost */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + (h.cost ?? 0), 0))}
                </td>
                {/* Native Value — spans mixed currencies, so it isn't summed */}
                {showNative && <td className="px-3 py-3 text-right tabular-nums text-xs text-ghost whitespace-nowrap">—</td>}
                {/* Cur Value */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0))}
                </td>
                {/* Prev Week */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-dim whitespace-nowrap">
                  {totalPrevWk ? fmtINR(totalPrevWk) : '—'}
                </td>
                {/* Wkly Chg % — the whole view's move over its own prior-week base,
                    not an average of the per-row percentages. */}
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={totalWeeklyPct} fmt={fmtPct} />
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
                {/* Returns YTD % / Inc % / CAGR / XIRR — all value-weighted */}
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
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {avgCagrAll != null
                    ? <span style={{ color: avgCagrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgCagrAll)} p.a.</span>
                    : <span className="text-ghost">—</span>}
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {avgXirrAll != null
                    ? <span style={{ color: avgXirrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgXirrAll)} p.a.</span>
                    : <span className="text-ghost">—</span>}
                </td>
                {/* FY Growth — Σpnl / Σbase over the rows that HAVE the year, so a
                    suppressed row doesn't drag the total toward zero. */}
                {fyLabels.map((l, i) => {
                  const t = fyTotal(rows, l);
                  return (
                    <td key={l}
                        className={`px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap ${i === 0 ? 'border-l border-rule' : ''}`}>
                      {t.pct != null
                        ? <span style={{ color: t.pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(t.pct)}</span>
                        : <span className="text-ghost">—</span>}
                    </td>
                  );
                })}
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
