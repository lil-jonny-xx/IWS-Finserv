'use client';
import { useState, Fragment, useMemo } from 'react';

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
}

export interface EquityTotals {
  total_cost?: number;
  total_current_market_value?: number;
  total_pnl_inception?: number;
  total_pnl_ytd?: number;
  total_weekly_change?: number;
  grand_total?: number;
}

interface Props {
  holdings: EquityHoldingRow[];
  totals: EquityTotals;
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
};

const BROKER_COLORS: Record<string, string> = {
  zerodha:   '#3772ff',
  angel_one: '#e05c00',
  dhan:      '#059669',
};

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
          <span className="text-[11px] text-ghost">Mkt Value {fmtINR(value)}</span>
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
        <Th col="current_market_value" label="Mkt Value"              rowSpan={2} />
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

export default function EquityTable({ holdings, totals, showEntityCol }: Props) {
  const [sortKey, setSortKey]         = useState<SortKey>('current_market_value');
  const [sortDir, setSortDir]         = useState<SortDir>('desc');
  const [search, setSearch]           = useState('');
  const [filterBroker, setFilterBroker]   = useState<string | null>(null);
  const [filterSector, setFilterSector]   = useState<string | null>(null);
  const [filterEntity, setFilterEntity]   = useState<string | null>(null);

  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No equity holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear after the first broker sync.</p>
      </div>
    );
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  const brokers     = [...new Set(holdings.map(h => h.broker))].sort();
  const sectors     = [...new Set(holdings.map(h => h.sector ?? 'Equity'))]
    .sort((a, b) => (sectorMeta(a).order - sectorMeta(b).order));
  const entityNames = [...new Set(holdings.map(h => h.entity_name).filter(Boolean) as string[])].sort();

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return holdings.filter(h => {
      if (filterBroker && h.broker                      !== filterBroker) return false;
      if (filterSector && (h.sector ?? 'Equity')        !== filterSector) return false;
      if (filterEntity && h.entity_name                 !== filterEntity) return false;
      if (q && !h.symbol.toLowerCase().includes(q) &&
               !(h.entity_name ?? '').toLowerCase().includes(q) &&
               !(h.isin ?? '').toLowerCase().includes(q) &&
               !BROKER_LABELS[h.broker]?.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [holdings, search, filterBroker, filterSector, filterEntity]);

  const rows = sortRows(filtered, sortKey, sortDir);

  // Group by sector, in defined order
  const activeSectors = [...new Set(rows.map(h => h.sector ?? 'Equity'))]
    .sort((a, b) => sectorMeta(a).order - sectorMeta(b).order);
  const bySector: Record<string, EquityHoldingRow[]> = {};
  for (const s of activeSectors) bySector[s] = rows.filter(h => (h.sector ?? 'Equity') === s);

  const asOfDate    = holdings.find(h => h.as_of_date)?.as_of_date;
  const avgCagrAll  = weightedAvgCagr(holdings);
  const totalPnlInc = holdings.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const totalPnlYtd = holdings.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0);
  const totalWeekly = holdings.reduce((s, h) => s + (h.weekly_change ?? 0), 0);
  const hasPnl      = holdings.some(h => h.pnl_inception != null);

  // col count: # + symbol + [entity] + exch + qty + avg_cost + cost + since + mkt_val + prev_wk + wkly_chg + exp% + pnl×3 + ret×3 + remarks
  const colCount = 2 + (showEntityCol ? 1 : 0) + 14;

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
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_cost)}</p>
          </div>
          <div>
            <p className="text-xs text-ghost mb-0.5">Market Value</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_current_market_value)}</p>
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
          <div>
            <p className="text-xs text-ghost mb-0.5">Holdings</p>
            <p className="text-sm font-semibold text-ink tabular-nums">{holdings.length}</p>
          </div>
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
        <FilterPills
          label="Sector"
          options={sectors}
          labelMap={Object.fromEntries(Object.entries(SECTOR_META).map(([k, v]) => [k, v.label]))}
          selected={filterSector}
          onChange={setFilterSector}
        />
        <FilterPills label="Broker" options={brokers} labelMap={BROKER_LABELS} selected={filterBroker} onChange={setFilterBroker} />
        {showEntityCol && <FilterPills label="Entity" options={entityNames} selected={filterEntity} onChange={setFilterEntity} />}
      </div>

      {/* Table */}
      <div className="overflow-auto max-h-[75vh]" role="region" aria-label="Equity holdings table" tabIndex={0}>
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
                          <p className="text-xs font-medium text-ink whitespace-nowrap">{h.symbol}</p>
                          <BrokerBadge broker={h.broker} />
                          {h.isin && <p className="text-[10px] text-ghost font-mono mt-0.5">{h.isin}</p>}
                        </td>
                        {showEntityCol && (
                          <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.entity_name ?? '—'}</td>
                        )}
                        <td className="px-3 py-3 text-xs text-ghost whitespace-nowrap align-top">{h.exchange ?? '—'}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{h.quantity.toFixed(0)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(h.avg_cost)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.cost)}</td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs align-top whitespace-nowrap">
                          <span className="text-ink">{fmtDate(h.first_invested_date)}</span>
                          {h.first_invested_date && <p className="text-[10px] text-ghost mt-0.5">{fmtDuration(h.first_invested_date)}</p>}
                        </td>
                        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.current_market_value)}</td>
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
                            : <span className="text-ghost">—</span>}
                        </td>
                        <td className="px-3 pr-5 sm:pr-6 py-3 text-xs text-ghost align-top max-w-[160px]">{h.remarks ?? '—'}</td>
                      </tr>
                    ))}
                  </Fragment>
                );
              })
            )}

            {/* Overall totals footer */}
            {rows.length > 0 && (
              <tr className="border-t-2 border-rule bg-page">
                <td colSpan={6 + (showEntityCol ? 1 : 0)} className="px-5 sm:px-6 py-3 text-xs font-semibold text-dim">
                  Total ({rows.length} holdings)
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + h.cost, 0))}
                </td>
                <td />
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold text-ink whitespace-nowrap">
                  {fmtINR(rows.reduce((s, h) => s + (h.current_market_value ?? 0), 0))}
                </td>
                <td /><td />
                <td className="px-3 py-3 text-right tabular-nums text-xs text-ghost whitespace-nowrap">—</td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap border-l border-rule">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  <ColorNum n={rows.reduce((s, h) => s + (h.pnl_weekly_change ?? 0), 0) || null} fmt={fmtINR} />
                </td>
                <td className="border-l border-rule" />
                <td />
                <td className="px-3 py-3 text-right tabular-nums text-xs font-semibold whitespace-nowrap">
                  {avgCagrAll != null
                    ? <span style={{ color: avgCagrAll >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(avgCagrAll)} p.a.</span>
                    : <span className="text-ghost">—</span>}
                </td>
                <td />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
