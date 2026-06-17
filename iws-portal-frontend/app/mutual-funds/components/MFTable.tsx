'use client';
import { useState, Fragment, useMemo, useEffect, useRef } from 'react';

export interface MFHoldingRow {
  id: number;
  isin: string;
  security_name: string;
  security_type: string;
  asset_class: string;
  amfi_code?: string;
  folio_number: string;
  quantity: number;
  avg_cost?: number;
  cost_basis?: number;
  invested_amount: number;
  nav?: number;
  current_value?: number;
  first_invested_date?: string;
  last_updated?: string;
  entity_name?: string;
  pan_group_name?: string;
  realized_gain?: number;
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
  xirr_inception_pct?: number;
  remarks?: string;
}

export interface MFTotals {
  total_holdings?: number;
  total_invested?: number;
  total_current_value?: number;
}

export interface CombinedSubRow {
  entity_name: string;
  folio_number: string;
  quantity: number;
  avg_cost?: number;
  invested_amount: number;
  nav?: number;
  current_value?: number;
  market_value_as_on?: number;
  pnl_inception?: number;
  xirr_inception_pct?: number;
  cagr_inception_pct?: number;
  first_invested_date?: string;
  realized_gain?: number;
}

export interface CombinedHolding {
  security_id: number;
  isin: string;
  security_name: string;
  security_type: string;
  asset_class: string;
  amfi_code?: string;
  quantity: number;
  avg_cost?: number;
  invested_amount: number;
  nav?: number;
  current_value?: number;
  market_value_as_on?: number;
  first_invested_date?: string;
  as_of_date?: string;
  exposure_pct?: number;
  weekly_change?: number;
  prev_week_value?: number;
  pnl_ytd?: number;
  pnl_inception?: number;
  pnl_weekly_change?: number;
  returns_ytd_pct?: number;
  returns_inception_pct?: number;
  cagr_inception_pct?: number;
  xirr_inception_pct?: number;
  realized_gain?: number;
  entities: string[];
  rows: CombinedSubRow[];
}

interface Props {
  holdings: MFHoldingRow[];
  totals: MFTotals;
  showEntityCol: boolean;
  viewMode: 'normal' | 'combined';
  onToggleCombined: () => void;
  combinedHoldings?: CombinedHolding[];
  combinedTotals?: { total_combined: number; total_invested: number };
  filterResetKey?: string | number | null;
}

// ── formatters ───────────────────────────────────────────────────────────────

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
  if (!iso) return '—';
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

function ColorNum({ n, fmt }: { n: number | null | undefined; fmt: (v: number) => string }) {
  if (n == null) return <span className="text-ghost">—</span>;
  return (
    <span style={{ color: n >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
      {fmt(n)}
    </span>
  );
}

// ── constants ─────────────────────────────────────────────────────────────────

const ASSET_CLASS_ORDER = ['EQUITY', 'HYBRID', 'ARBITRAGE', 'ALTERNATES', 'FIXED_INCOME'] as const;
const ASSET_CLASS_LABELS: Record<string, string> = {
  EQUITY:       'Equity',
  HYBRID:       'Hybrid',
  ARBITRAGE:    'Arbitrage',
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

type SortKey = keyof MFHoldingRow;
type SortDir = 'asc' | 'desc';
type GroupMode = 'entity' | 'pan';

// ── weighted averages ─────────────────────────────────────────────────────────

function weightedAvg(rows: MFHoldingRow[], key: 'cagr_inception_pct' | 'xirr_inception_pct'): number | null {
  let sumWeight = 0, sumWeighted = 0;
  for (const h of rows) {
    if (h[key] == null) continue;
    const w = h.market_value_as_on ?? h.current_value ?? 0;
    sumWeighted += (h[key] as number) * w;
    sumWeight += w;
  }
  return sumWeight > 0 ? sumWeighted / sumWeight : null;
}

// ── sort ──────────────────────────────────────────────────────────────────────

function sortRows(rows: MFHoldingRow[], key: SortKey, dir: SortDir): MFHoldingRow[] {
  return [...rows].sort((a, b) => {
    const va = (a[key] as number | string) ?? (typeof a[key] === 'number' ? -Infinity : '');
    const vb = (b[key] as number | string) ?? (typeof b[key] === 'number' ? -Infinity : '');
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

// ── client-side combined merge (by ISIN, across all entities) ────────────────

function mergeMFByIsin(holdings: MFHoldingRow[]): CombinedHolding[] {
  const map = new Map<string, MFHoldingRow[]>();
  for (const h of holdings) {
    const key = h.isin || String(h.id);
    if (!map.has(key)) map.set(key, []);
    map.get(key)!.push(h);
  }

  const result: CombinedHolding[] = [];
  for (const rows of map.values()) {
    const totalQty  = rows.reduce((s, h) => s + h.quantity, 0);
    const totalInv  = rows.reduce((s, h) => s + h.invested_amount, 0);
    const totalMkt  = rows.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0);
    const totalCurr = rows.reduce((s, h) => s + (h.current_value ?? 0), 0);
    const wavgCost  = totalQty > 0
      ? rows.reduce((s, h) => s + h.quantity * (h.avg_cost ?? 0), 0) / totalQty
      : undefined;

    const pnlYtd  = rows.some(h => h.pnl_ytd != null)          ? rows.reduce((s, h) => s + (h.pnl_ytd ?? 0), 0)           : undefined;
    const pnlInc  = rows.some(h => h.pnl_inception != null)     ? rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0)     : undefined;
    const pnlWkly = rows.some(h => h.pnl_weekly_change != null) ? rows.reduce((s, h) => s + (h.pnl_weekly_change ?? 0), 0) : undefined;
    const wklyChg = rows.some(h => h.weekly_change != null)     ? rows.reduce((s, h) => s + (h.weekly_change ?? 0), 0)     : undefined;
    const prevWk  = rows.some(h => h.prev_week_value != null)   ? rows.reduce((s, h) => s + (h.prev_week_value ?? 0), 0)   : undefined;
    const expPct  = rows.some(h => h.exposure_pct != null)      ? rows.reduce((s, h) => s + (h.exposure_pct ?? 0), 0)      : undefined;
    const realized = rows.reduce((s, h) => s + (h.realized_gain ?? 0), 0);

    const returnsInc = totalInv > 0 && pnlInc != null ? (pnlInc / totalInv) * 100 : undefined;

    const dates = rows.map(h => h.first_invested_date).filter(Boolean) as string[];
    const firstDate = dates.length > 0 ? [...dates].sort()[0] : undefined;
    let cagrInc: number | undefined;
    if (firstDate && totalInv > 0 && totalMkt > 0) {
      const years = (Date.now() - new Date(firstDate).getTime()) / (365.25 * 24 * 3600 * 1000);
      if (years >= 0.01) cagrInc = (Math.pow(totalMkt / totalInv, 1 / years) - 1) * 100;
    }

    const xirrRows = rows.filter(h => h.xirr_inception_pct != null);
    let xirrVal: number | undefined;
    if (xirrRows.length > 0) {
      const totalW = xirrRows.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0);
      if (totalW > 0) {
        xirrVal = xirrRows.reduce((s, h) => s + (h.xirr_inception_pct! * (h.market_value_as_on ?? h.current_value ?? 0)), 0) / totalW;
      }
    }

    const entities = [...new Set(rows.map(h => h.entity_name).filter(Boolean) as string[])].sort();

    const subRows: CombinedSubRow[] = rows.map(h => ({
      entity_name:         h.entity_name ?? '',
      folio_number:        h.folio_number,
      quantity:            h.quantity,
      avg_cost:            h.avg_cost,
      invested_amount:     h.invested_amount,
      nav:                 h.nav,
      current_value:       h.current_value,
      market_value_as_on:  h.market_value_as_on,
      pnl_inception:       h.pnl_inception,
      xirr_inception_pct:  h.xirr_inception_pct,
      cagr_inception_pct:  h.cagr_inception_pct,
      first_invested_date: h.first_invested_date,
      realized_gain:       h.realized_gain,
    }));

    result.push({
      security_id:           rows[0].id,
      isin:                  rows[0].isin,
      security_name:         rows[0].security_name,
      security_type:         rows[0].security_type,
      asset_class:           rows[0].asset_class,
      amfi_code:             rows[0].amfi_code,
      quantity:              totalQty,
      avg_cost:              wavgCost,
      invested_amount:       totalInv,
      nav:                   rows[0].nav,
      current_value:         totalCurr,
      market_value_as_on:    totalMkt || undefined,
      first_invested_date:   firstDate,
      as_of_date:            rows[0].as_of_date,
      exposure_pct:          expPct,
      weekly_change:         wklyChg,
      prev_week_value:       prevWk,
      pnl_ytd:               pnlYtd,
      pnl_inception:         pnlInc,
      pnl_weekly_change:     pnlWkly,
      returns_ytd_pct:       undefined,
      returns_inception_pct: returnsInc,
      cagr_inception_pct:    cagrInc,
      xirr_inception_pct:    xirrVal,
      realized_gain:         realized || undefined,
      entities,
      rows: subRows,
    });
  }
  return result;
}

// ── filter pills ─────────────────────────────────────────────────────────────

function FilterPills({
  label, options, selected, onChange,
}: {
  label: string;
  options: string[];
  selected: string | null;
  onChange: (v: string | null) => void;
}) {
  if (options.length < 2) return null;
  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-[10px] text-ghost font-medium shrink-0">{label}:</span>
      {[{ value: null, label: 'All' }, ...options.map(o => ({ value: o, label: ASSET_CLASS_LABELS[o] ?? SEC_TYPE_LABELS[o] ?? o }))].map(opt => (
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

// ── group header row ──────────────────────────────────────────────────────────

function GroupHeader({
  label, rows, colCount,
}: {
  label: string; rows: MFHoldingRow[]; colCount: number;
}) {
  const invested    = rows.reduce((s, h) => s + h.invested_amount, 0);
  const mktVal      = rows.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0);
  const pnl         = rows.reduce((s, h) => s + (h.pnl_inception ?? 0), 0);
  const realized    = rows.reduce((s, h) => s + (h.realized_gain ?? 0), 0);
  const avgCagr     = weightedAvg(rows, 'cagr_inception_pct');
  const hasPnl      = rows.some(h => h.pnl_inception != null);

  return (
    <tr>
      <td colSpan={colCount} className="px-4 pl-5 sm:pl-6 py-2.5 bg-page border-t border-rule">
        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <span className="text-xs font-semibold text-ink">{label}</span>
          <span className="text-[11px] text-ghost">Invested {fmtINR(invested)}</span>
          <span className="text-[11px] text-ghost">Mkt Value {fmtINR(mktVal)}</span>
          {hasPnl && (
            <span className="text-[11px] font-medium" style={{ color: pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              P&amp;L {pnl >= 0 ? '+' : ''}{fmtINR(pnl)}
            </span>
          )}
          {realized > 0 && (
            <span className="text-[11px] text-ghost">Realized {fmtINR(realized)}</span>
          )}
          {avgCagr != null && (
            <span className="text-[11px] font-medium" style={{ color: avgCagr >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              Avg CAGR {fmtPct(avgCagr)} p.a.
            </span>
          )}
        </div>
      </td>
    </tr>
  );
}

// ── asset class sub-header ────────────────────────────────────────────────────

function AssetClassHeader({ cls, rows, colCount }: { cls: string; rows: MFHoldingRow[]; colCount: number }) {
  const subtotal = rows.reduce((s, h) => s + (h.market_value_as_on ?? h.current_value ?? 0), 0);
  return (
    <tr>
      <td colSpan={colCount} className="px-4 pl-8 py-1.5 bg-page/60">
        <span className="text-[10px] font-semibold text-dim uppercase tracking-wide">
          {ASSET_CLASS_LABELS[cls] ?? cls}
        </span>
        <span className="ml-2 text-[10px] text-ghost">{fmtINR(subtotal)}</span>
      </td>
    </tr>
  );
}

// ── table headers ─────────────────────────────────────────────────────────────

function TableHead({
  showEntityCol, showPanCol, sortKey, sortDir, onSort,
}: {
  showEntityCol: boolean;
  showPanCol: boolean;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
}) {
  const base = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap sticky top-0 z-10';

  function Th({ col, label, right = true, rowSpan = 1, colSpan = 1, borderL = false, first = false, last = false }: {
    col: SortKey; label: string; right?: boolean; rowSpan?: number; colSpan?: number;
    borderL?: boolean; first?: boolean; last?: boolean;
  }) {
    return (
      <th scope="col" rowSpan={rowSpan} colSpan={colSpan}
        className={`${base} ${right ? 'text-right' : 'text-left'} ${borderL ? 'border-l border-rule' : ''} ${first ? 'pl-5 sm:pl-6' : ''} ${last ? 'pr-5 sm:pr-6' : ''}`}
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
      <th scope="col" colSpan={colSpan}
        className={`${base} text-center ${borderL ? 'border-l border-rule' : ''}`}>
        {label}
      </th>
    );
  }

  return (
    <thead>
      <tr>
        {/* Sr. No. */}
        <th scope="col" rowSpan={2} className={`${base} text-right pl-5 sm:pl-6 w-8`}>#</th>
        <Th col="security_name"     label="Fund"          right={false} rowSpan={2} />
        {showEntityCol && <Th col="entity_name"  label="Entity"        right={false} rowSpan={2} />}
        {showPanCol    && <Th col="pan_group_name" label="PAN"         right={false} rowSpan={2} />}
        <Th col="folio_number"      label="Folio"         right={false} rowSpan={2} />
        <Th col="quantity"          label="Units"                       rowSpan={2} />
        <Th col="nav"               label="NAV"                         rowSpan={2} />
        <Th col="invested_amount"   label="Cost"                        rowSpan={2} />
        <Th col="first_invested_date" label="Since"                     rowSpan={2} />
        <Th col="exposure_pct"      label="Exp %"                       rowSpan={2} />
        <Th col="market_value_as_on" label="Mkt Value"                  rowSpan={2} />
        <Th col="prev_week_value"   label="Prev Week"                   rowSpan={2} />
        <Th col="weekly_change"     label="Wkly Chg"                    rowSpan={2} />
        {/* P&L group */}
        <StaticTh label="P&L" colSpan={3} borderL />
        {/* Returns group */}
        <StaticTh label="Returns" colSpan={4} borderL />
        {/* Realized */}
        <th scope="col" rowSpan={2} className={`${base} text-right border-l border-rule`}>Realized</th>
        <th scope="col" rowSpan={2} className={`${base} text-left pr-5 sm:pr-6`}>Remarks</th>
      </tr>
      <tr>
        {/* P&L sub-headers */}
        <Th col="pnl_ytd"             label="YTD"      borderL />
        <Th col="pnl_inception"       label="Inception" />
        <Th col="pnl_weekly_change"   label="Wkly Chg" />
        {/* Returns sub-headers */}
        <Th col="returns_ytd_pct"     label="YTD %"    borderL />
        <Th col="returns_inception_pct" label="Inc %" />
        <Th col="cagr_inception_pct"  label="CAGR" />
        <Th col="xirr_inception_pct"  label="XIRR" />
      </tr>
    </thead>
  );
}

// ── data row ──────────────────────────────────────────────────────────────────

function DataRow({
  h, srNo, showEntityCol, showPanCol,
}: {
  h: MFHoldingRow; srNo: number; showEntityCol: boolean; showPanCol: boolean;
}) {
  const mktVal = h.market_value_as_on ?? h.current_value;
  return (
    <tr className="border-t border-rule hover:bg-page transition-colors duration-100">
      <td className="px-3 pl-5 sm:pl-6 py-3 text-right tabular-nums text-xs text-ghost align-top">{srNo}</td>
      <td className="px-3 py-3 align-top">
        <p className="text-xs text-ink leading-snug">{h.security_name}</p>
        <p className="text-[10px] text-ghost mt-0.5">{SEC_TYPE_LABELS[h.security_type] ?? h.security_type}</p>
      </td>
      {showEntityCol && (
        <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.entity_name ?? '—'}</td>
      )}
      {showPanCol && (
        <td className="px-3 py-3 text-xs font-medium text-dim whitespace-nowrap align-top">{h.pan_group_name ?? '—'}</td>
      )}
      <td className="px-3 py-3 font-mono text-xs text-dim whitespace-nowrap align-top">{h.folio_number}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{h.quantity.toFixed(3)}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{h.nav != null ? h.nav.toFixed(4) : '—'}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.invested_amount)}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs align-top whitespace-nowrap">
        <span className="text-ink">{fmtDate(h.first_invested_date)}</span>
        {h.first_invested_date && (
          <p className="text-[10px] text-ghost mt-0.5">{fmtDuration(h.first_invested_date)}</p>
        )}
      </td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
        {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
      </td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(mktVal)}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(h.prev_week_value)}</td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.weekly_change} fmt={fmtINR} /></td>
      {/* P&L */}
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.pnl_ytd} fmt={fmtINR} /></td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_inception} fmt={fmtINR} /></td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_weekly_change} fmt={fmtINR} /></td>
      {/* Returns */}
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.returns_ytd_pct} fmt={fmtPct} /></td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.returns_inception_pct} fmt={fmtPct} /></td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
        {h.cagr_inception_pct != null
          ? <span style={{ color: h.cagr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.cagr_inception_pct)} p.a.</span>
          : <span className="text-ghost">—</span>}
      </td>
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
        {h.xirr_inception_pct != null
          ? <span style={{ color: h.xirr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.xirr_inception_pct)} p.a.</span>
          : <span className="text-ghost">—</span>}
      </td>
      {/* Realized */}
      <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule">
        {(h.realized_gain ?? 0) > 0
          ? <span className="text-dim">{fmtINR(h.realized_gain)}</span>
          : <span className="text-ghost">—</span>}
      </td>
      <td className="px-3 pr-5 sm:pr-6 py-3 text-xs text-ghost align-top max-w-[160px]">{h.remarks ?? '—'}</td>
    </tr>
  );
}

// ── combined table header ─────────────────────────────────────────────────────

const COMBINED_COL_COUNT = 18; // expand + # + fund + entities + units + nav + cost + since + exp% + mktval + prevwk + wklychg + pnl×3 + ret×2(inc%+xirr) + realized

function CombinedTableHead({
  sortKey, sortDir, onSort,
}: {
  sortKey: keyof CombinedHolding;
  sortDir: SortDir;
  onSort: (k: keyof CombinedHolding) => void;
}) {
  const base = 'px-3 py-2.5 text-xs font-medium text-ghost bg-card border-b border-rule whitespace-nowrap sticky top-0 z-10';

  function Th({ col, label, right = true, rowSpan = 1, colSpan = 1, borderL = false, first = false }: {
    col: keyof CombinedHolding; label: string; right?: boolean;
    rowSpan?: number; colSpan?: number; borderL?: boolean; first?: boolean;
  }) {
    return (
      <th scope="col" rowSpan={rowSpan} colSpan={colSpan}
        className={`${base} ${right ? 'text-right' : 'text-left'} ${borderL ? 'border-l border-rule' : ''} ${first ? 'pl-5 sm:pl-6' : ''}`}
        aria-sort={sortKey === col ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}
      >
        <button onClick={() => onSort(col)} className={`inline-flex items-center hover:text-ink transition-colors ${right ? 'ml-auto' : ''}`}>
          {label}<SortArrow col={col as SortKey} sortKey={sortKey as SortKey} sortDir={sortDir} />
        </button>
      </th>
    );
  }
  function StaticTh({ label, colSpan = 1, borderL = false }: { label: string; colSpan?: number; borderL?: boolean }) {
    return (
      <th scope="col" colSpan={colSpan}
        className={`${base} text-center ${borderL ? 'border-l border-rule' : ''}`}>
        {label}
      </th>
    );
  }

  return (
    <thead>
      <tr>
        {/* expand arrow */}
        <th scope="col" rowSpan={2} className={`${base} w-6 pl-3`} />
        <th scope="col" rowSpan={2} className={`${base} text-right pl-2 w-8`}>#</th>
        <Th col="security_name"        label="Fund"         right={false} rowSpan={2} first />
        <Th col="entities"             label="Entities"     right={false} rowSpan={2} />
        <Th col="quantity"             label="Units"                      rowSpan={2} />
        <Th col="nav"                  label="NAV"                        rowSpan={2} />
        <Th col="invested_amount"      label="Cost"                       rowSpan={2} />
        <Th col="first_invested_date"  label="Since"                      rowSpan={2} />
        <Th col="exposure_pct"         label="Exp %"                      rowSpan={2} />
        <Th col="market_value_as_on"   label="Mkt Value"                  rowSpan={2} />
        <Th col="prev_week_value"      label="Prev Week"                  rowSpan={2} />
        <Th col="weekly_change"        label="Wkly Chg"                   rowSpan={2} />
        <StaticTh label="P&L"    colSpan={3} borderL />
        <StaticTh label="Returns" colSpan={2} borderL />
        <th scope="col" rowSpan={2} className={`${base} text-right border-l border-rule pr-5 sm:pr-6`}>Realized</th>
      </tr>
      <tr>
        <Th col="pnl_ytd"              label="YTD"     borderL />
        <Th col="pnl_inception"        label="Inception" />
        <Th col="pnl_weekly_change"    label="Wkly Chg" />
        <Th col="returns_inception_pct" label="Inc %"  borderL />
        <Th col="xirr_inception_pct"   label="XIRR" />
      </tr>
    </thead>
  );
}

// ── combined data row ─────────────────────────────────────────────────────────

function CombinedDataRow({
  h, srNo, expanded, onToggle,
}: {
  h: CombinedHolding; srNo: number; expanded: boolean; onToggle: () => void;
}) {
  const mktVal = h.market_value_as_on ?? h.current_value;
  return (
    <Fragment>
      <tr
        className="border-t border-rule hover:bg-page transition-colors duration-100 cursor-pointer select-none"
        onClick={onToggle}
      >
        <td className="pl-3 pr-1 py-3 text-center text-ghost text-[10px] align-top">
          <span className="inline-block transition-transform duration-150" style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>▶</span>
        </td>
        <td className="px-2 py-3 text-right tabular-nums text-xs text-ghost align-top">{srNo}</td>
        <td className="px-3 pl-5 sm:pl-6 py-3 align-top">
          <p className="text-xs text-ink leading-snug">{h.security_name}</p>
          <p className="text-[10px] text-ghost mt-0.5">{SEC_TYPE_LABELS[h.security_type] ?? h.security_type}</p>
        </td>
        <td className="px-3 py-3 align-top">
          <div className="flex flex-wrap gap-1">
            {h.entities.map(e => (
              <span key={e} className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-page border border-rule text-dim">{e}</span>
            ))}
          </div>
        </td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{h.quantity.toFixed(3)}</td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{h.nav != null ? h.nav.toFixed(4) : '—'}</td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(h.invested_amount)}</td>
        <td className="px-3 py-3 text-right tabular-nums text-xs align-top whitespace-nowrap">
          <span className="text-ink">{fmtDate(h.first_invested_date)}</span>
          {h.first_invested_date && <p className="text-[10px] text-ghost mt-0.5">{fmtDuration(h.first_invested_date)}</p>}
        </td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">
          {h.exposure_pct != null ? h.exposure_pct.toFixed(2) + '%' : '—'}
        </td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-ink whitespace-nowrap align-top">{fmtINR(mktVal)}</td>
        <td className="px-3 py-3 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(h.prev_week_value)}</td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.weekly_change} fmt={fmtINR} /></td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.pnl_ytd} fmt={fmtINR} /></td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_inception} fmt={fmtINR} /></td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={h.pnl_weekly_change} fmt={fmtINR} /></td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule"><ColorNum n={h.returns_inception_pct} fmt={fmtPct} /></td>
        <td className="px-3 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top">
          {h.xirr_inception_pct != null
            ? <span style={{ color: h.xirr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(h.xirr_inception_pct)} p.a.</span>
            : <span className="text-ghost">—</span>}
        </td>
        <td className="px-3 pr-5 sm:pr-6 py-3 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule">
          {(h.realized_gain ?? 0) > 0
            ? <span className="text-dim">{fmtINR(h.realized_gain)}</span>
            : <span className="text-ghost">—</span>}
        </td>
      </tr>
      {expanded && h.rows.map((sub, i) => (
        <CombinedSubRowEl key={sub.entity_name + sub.folio_number + i} sub={sub} />
      ))}
    </Fragment>
  );
}

// ── combined sub-row ──────────────────────────────────────────────────────────

function CombinedSubRowEl({ sub }: { sub: CombinedSubRow }) {
  const mktVal = sub.market_value_as_on ?? sub.current_value;
  return (
    <tr className="border-t border-rule/40 bg-page/50">
      <td className="pl-3 pr-1 py-2" />
      <td className="px-2 py-2" />
      <td className="px-3 pl-8 py-2 align-top" colSpan={1}>
        <p className="text-[11px] font-medium text-dim">{sub.entity_name}</p>
        <p className="text-[10px] text-ghost font-mono mt-0.5">{sub.folio_number}</p>
      </td>
      {/* entities col — shows this entity's badge */}
      <td className="px-3 py-2 align-top">
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-card border border-rule text-ghost">{sub.entity_name}</span>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{sub.quantity.toFixed(3)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">{sub.nav != null ? sub.nav.toFixed(4) : '—'}</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(sub.invested_amount)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">{fmtDate(sub.first_invested_date)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-dim whitespace-nowrap align-top">{fmtINR(mktVal)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule/40">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs whitespace-nowrap align-top"><ColorNum n={sub.pnl_inception} fmt={fmtINR} /></td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs text-ghost whitespace-nowrap align-top border-l border-rule/40">—</td>
      <td className="px-3 py-2 text-right tabular-nums text-xs whitespace-nowrap align-top">
        {sub.xirr_inception_pct != null
          ? <span style={{ color: sub.xirr_inception_pct >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{fmtPct(sub.xirr_inception_pct)} p.a.</span>
          : <span className="text-ghost">—</span>}
      </td>
      <td className="px-3 pr-5 sm:pr-6 py-2 text-right tabular-nums text-xs whitespace-nowrap align-top border-l border-rule/40">
        {(sub.realized_gain ?? 0) > 0
          ? <span className="text-ghost">{fmtINR(sub.realized_gain)}</span>
          : <span className="text-ghost">—</span>}
      </td>
    </tr>
  );
}

// ── main component ────────────────────────────────────────────────────────────

export default function MFTable({ holdings, totals, showEntityCol, viewMode, onToggleCombined, combinedHoldings, combinedTotals, filterResetKey }: Props) {
  const [sortKey, setSortKey]       = useState<SortKey>('market_value_as_on');
  const [sortDir, setSortDir]       = useState<SortDir>('desc');
  const [search, setSearch]         = useState('');
  const [groupMode, setGroupMode]   = useState<GroupMode>('entity');
  const [filterClass, setFilterClass]   = useState<string | null>(null);
  const [filterType, setFilterType]     = useState<string | null>(null);
  const [filterEntity, setFilterEntity] = useState<string | null>(null);
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());

  // Reset all filters when the entity tab changes so stale filters from one
  // entity view don't bleed into another (e.g. filterEntity='IWS' hiding all
  // rows when switching back to the All tab, or to a different entity).
  const prevResetKey = useRef(filterResetKey);
  useEffect(() => {
    if (filterResetKey !== prevResetKey.current) {
      prevResetKey.current = filterResetKey;
      setSearch('');
      setFilterClass(null);
      setFilterType(null);
      setFilterEntity(null);
    }
  }, [filterResetKey]);

  function toggleExpand(secId: number) {
    setExpandedRows(prev => {
      const next = new Set(prev);
      if (next.has(secId)) next.delete(secId); else next.add(secId);
      return next;
    });
  }

  // Compute combined locally from existing holdings (instant, no separate fetch required).
  // Once the API responds, switch to its data — it has pooled XIRR, CAGR, and P&L from
  // actual transactions across all entities, which is more accurate than the client-side merge.
  const localCombined = useMemo(() => mergeMFByIsin(holdings), [holdings]);
  const effectiveCombined = useMemo(() => {
    if (combinedHoldings && combinedHoldings.length > 0) return combinedHoldings;
    return localCombined;
  }, [localCombined, combinedHoldings]);

  if (holdings.length === 0) {
    return (
      <div className="bg-card rounded-lg border border-rule px-6 py-12 text-center">
        <p className="text-sm font-medium text-ink mb-1">No mutual fund holdings on record</p>
        <p className="text-xs text-ghost">Holdings will appear once your CAS statement has been imported.</p>
      </div>
    );
  }

  function handleSort(key: SortKey) {
    if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    else { setSortKey(key); setSortDir('desc'); }
  }

  // unique filter option values
  const assetClasses  = [...new Set(holdings.map(h => h.asset_class))].sort();
  const secTypes      = [...new Set(holdings.map(h => h.security_type))].sort();
  const entityNames   = [...new Set(holdings.map(h => h.entity_name).filter(Boolean) as string[])].sort();

  // apply all filters
  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return holdings.filter(h => {
      if (filterClass  && h.asset_class    !== filterClass)  return false;
      if (filterType   && h.security_type  !== filterType)   return false;
      if (filterEntity && h.entity_name    !== filterEntity) return false;
      if (q && !h.security_name.toLowerCase().includes(q) &&
               !h.folio_number.toLowerCase().includes(q) &&
               !(h.entity_name ?? '').toLowerCase().includes(q)) return false;
      return true;
    });
  }, [holdings, search, filterClass, filterType, filterEntity]);

  const asOfDate   = holdings.find(h => h.as_of_date)?.as_of_date;
  const avgCagr    = weightedAvg(holdings, 'cagr_inception_pct');
  const avgXirr    = weightedAvg(holdings, 'xirr_inception_pct');
  const hasMath    = holdings.some(h => h.pnl_inception != null);

  const totalPnlInception  = holdings.reduce((s, h) => s + (h.pnl_inception  ?? 0), 0);
  const totalPnlYtd        = holdings.reduce((s, h) => s + (h.pnl_ytd        ?? 0), 0);
  const totalWeeklyChg     = holdings.reduce((s, h) => s + (h.weekly_change  ?? 0), 0);
  const totalRealized      = holdings.reduce((s, h) => s + (h.realized_gain ?? 0), 0);

  // show PAN col only when groupMode=entity AND admin all-entities; in PAN mode grouping replaces it
  const showPanCol  = showEntityCol && groupMode === 'entity';
  const colCount    = 3                          // sr + fund + folio
    + (showEntityCol && groupMode === 'entity' ? 1 : 0)
    + (showPanCol ? 1 : 0)
    + 8                                          // units nav cost since exp% mktval prevwk wklychg
    + 7                                          // pnl×3 returns×4
    + 2;                                         // realized remarks

  // group rows
  type GroupEntry = { key: string; label: string; rows: MFHoldingRow[] };

  const groups: GroupEntry[] = useMemo(() => {
    if (groupMode === 'pan') {
      const map = new Map<string, MFHoldingRow[]>();
      for (const h of filtered) {
        const key = h.pan_group_name ?? 'Unknown';
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push(h);
      }
      return [...map.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, rows]) => ({
        key, label: key, rows,
      }));
    }
    // entity grouping
    if (showEntityCol) {
      const map = new Map<string, MFHoldingRow[]>();
      for (const h of filtered) {
        const key = h.entity_name ?? 'Unknown';
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push(h);
      }
      return [...map.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, rows]) => ({
        key, label: key, rows,
      }));
    }
    // single entity — one group, no header
    return [{ key: 'all', label: '', rows: filtered }];
  }, [filtered, groupMode, showEntityCol]);

  // serial number is global across all groups
  let srCounter = 0;

  return (
    <div className="bg-card rounded-lg border border-rule overflow-hidden">

      {/* Summary strip */}
      <div className="px-5 sm:px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3 mb-3">
          <h2 className="text-base font-semibold text-ink">
            Mutual Fund Holdings
            {asOfDate && <span className="ml-3 text-xs font-normal text-ghost">as of {fmtDate(asOfDate)}</span>}
          </h2>
        </div>
        <div className="flex flex-wrap gap-x-8 gap-y-3">
          {totals.total_invested != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Total Invested</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_invested)}</p>
            </div>
          )}
          {totals.total_current_value != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Market Value</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{fmtINR(totals.total_current_value)}</p>
            </div>
          )}
          {hasMath && (
            <>
              <div>
                <p className="text-xs text-ghost mb-0.5">P&amp;L (Inception)</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalPnlInception >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalPnlInception >= 0 ? '+' : ''}{fmtINR(totalPnlInception)}
                </p>
              </div>
              <div>
                <p className="text-xs text-ghost mb-0.5">P&amp;L YTD</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalPnlYtd >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalPnlYtd >= 0 ? '+' : ''}{fmtINR(totalPnlYtd)}
                </p>
              </div>
              <div>
                <p className="text-xs text-ghost mb-0.5">Weekly Change</p>
                <p className="text-sm font-semibold tabular-nums"
                  style={{ color: totalWeeklyChg >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                  {totalWeeklyChg >= 0 ? '+' : ''}{fmtINR(totalWeeklyChg)}
                </p>
              </div>
              {avgXirr != null && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Avg XIRR</p>
                  <p className="text-sm font-semibold tabular-nums"
                    style={{ color: avgXirr >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                    {fmtPct(avgXirr)} p.a.
                  </p>
                </div>
              )}
              {avgCagr != null && (
                <div>
                  <p className="text-xs text-ghost mb-0.5">Avg CAGR</p>
                  <p className="text-sm font-semibold tabular-nums"
                    style={{ color: avgCagr >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                    {fmtPct(avgCagr)} p.a.
                  </p>
                </div>
              )}
            </>
          )}
          {totalRealized > 0 && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Total Realized</p>
              <p className="text-sm font-semibold text-dim tabular-nums">{fmtINR(totalRealized)}</p>
            </div>
          )}
          {totals.total_holdings != null && (
            <div>
              <p className="text-xs text-ghost mb-0.5">Holdings</p>
              <p className="text-sm font-semibold text-ink tabular-nums">{totals.total_holdings}</p>
            </div>
          )}
        </div>
      </div>

      {/* Toolbar: search + grouping toggle + filters */}
      <div className="px-5 sm:px-6 py-3 border-b border-rule flex flex-wrap gap-3 items-start">
        {/* Search */}
        <input
          type="search"
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Search fund, folio or entity…"
          aria-label="Search mutual fund holdings"
          className="text-xs bg-page border border-wire rounded px-3 py-1.5 text-ink placeholder:text-ghost focus:outline-none focus:border-prime transition-colors w-52 shrink-0"
        />

        {/* Combined toggle */}
        {showEntityCol && (
          <button
            onClick={onToggleCombined}
            className={`px-3 py-1.5 rounded text-xs font-medium border transition-colors shrink-0 ${
              viewMode === 'combined'
                ? 'bg-prime text-prime-fg border-prime'
                : 'bg-page border-wire text-dim hover:border-dim hover:text-ink'
            }`}
            aria-pressed={viewMode === 'combined'}
          >
            Combined
          </button>
        )}

        {/* Group-by toggle */}
        {showEntityCol && viewMode === 'normal' && (
          <div className="flex items-center gap-1 bg-page border border-wire rounded p-0.5 shrink-0">
            <span className="text-[10px] text-ghost px-1.5 font-medium">Group by</span>
            {(['entity', 'pan'] as GroupMode[]).map(m => (
              <button
                key={m}
                onClick={() => setGroupMode(m)}
                className={`px-2.5 py-1 rounded text-[10px] font-medium transition-colors ${
                  groupMode === m
                    ? 'bg-prime text-prime-fg'
                    : 'text-dim hover:text-ink'
                }`}
              >
                {m === 'entity' ? 'Entity' : 'PAN'}
              </button>
            ))}
          </div>
        )}

        {/* Filter pills */}
        <div className="flex flex-wrap gap-x-5 gap-y-2 items-center">
          <FilterPills
            label="Class"
            options={assetClasses}
            selected={filterClass}
            onChange={setFilterClass}
          />
          <FilterPills
            label="Type"
            options={secTypes}
            selected={filterType}
            onChange={setFilterType}
          />
          {showEntityCol && viewMode === 'normal' && (
            <FilterPills
              label="Entity"
              options={entityNames}
              selected={filterEntity}
              onChange={setFilterEntity}
            />
          )}
        </div>
      </div>

      {/* Table */}
      <div className="overflow-x-auto" role="region" aria-label="MF holdings table" tabIndex={0}>

        {/* ── Combined mode ── */}
        {viewMode === 'combined' && (
          <table className="w-full text-sm" style={{ minWidth: '1900px' }}>
            <CombinedTableHead
              sortKey={(sortKey as unknown) as keyof CombinedHolding}
              sortDir={sortDir}
              onSort={k => {
                const key = k as unknown as SortKey;
                if (key === sortKey) setSortDir(d => d === 'asc' ? 'desc' : 'asc');
                else { setSortKey(key); setSortDir('desc'); }
              }}
            />
            <tbody>
              {effectiveCombined.length === 0 ? (
                <tr>
                  <td colSpan={COMBINED_COL_COUNT} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                    No combined holdings data.
                  </td>
                </tr>
              ) : (() => {
                const q = search.toLowerCase();
                const filtered2 = effectiveCombined.filter(h => {
                  if (filterClass && h.asset_class !== filterClass) return false;
                  if (filterType  && h.security_type !== filterType)  return false;
                  if (q && !h.security_name.toLowerCase().includes(q) &&
                           !h.entities.join(' ').toLowerCase().includes(q)) return false;
                  return true;
                }).sort((a, b) => {
                  const va = (a[sortKey as keyof CombinedHolding] as number | string) ?? (typeof a[sortKey as keyof CombinedHolding] === 'number' ? -Infinity : '');
                  const vb = (b[sortKey as keyof CombinedHolding] as number | string) ?? (typeof b[sortKey as keyof CombinedHolding] === 'number' ? -Infinity : '');
                  if (va < vb) return sortDir === 'asc' ? -1 : 1;
                  if (va > vb) return sortDir === 'asc' ? 1 : -1;
                  return 0;
                });
                // group by asset class
                const byClass = new Map<string, CombinedHolding[]>();
                for (const h of filtered2) {
                  if (!byClass.has(h.asset_class)) byClass.set(h.asset_class, []);
                  byClass.get(h.asset_class)!.push(h);
                }
                let srC = 0;
                return ASSET_CLASS_ORDER.filter(cls => byClass.has(cls)).map(cls => {
                  const clsRows = byClass.get(cls)!;
                  return (
                    <Fragment key={cls}>
                      <tr>
                        <td colSpan={COMBINED_COL_COUNT} className="px-4 pl-5 sm:pl-6 py-1.5 bg-page/60">
                          <span className="text-[10px] font-semibold text-dim uppercase tracking-wide">
                            {ASSET_CLASS_LABELS[cls] ?? cls}
                          </span>
                          <span className="ml-2 text-[10px] text-ghost">
                            {fmtINR(clsRows.reduce((s, h) => s + (h.market_value_as_on ?? 0), 0))}
                          </span>
                        </td>
                      </tr>
                      {clsRows.map(h => (
                        <CombinedDataRow
                          key={h.security_id}
                          h={h}
                          srNo={++srC}
                          expanded={expandedRows.has(h.security_id)}
                          onToggle={() => toggleExpand(h.security_id)}
                        />
                      ))}
                    </Fragment>
                  );
                });
              })()}
            </tbody>
          </table>
        )}

        {/* ── Normal mode ── */}
        {viewMode === 'normal' && (
          <table className="w-full text-sm" style={{ minWidth: '1800px' }}>
            <TableHead
              showEntityCol={showEntityCol && groupMode === 'entity'}
              showPanCol={showPanCol}
              sortKey={sortKey}
              sortDir={sortDir}
              onSort={handleSort}
            />
            <tbody>
              {filtered.length === 0 ? (
                <tr>
                  <td colSpan={colCount} className="px-5 sm:px-6 py-8 text-center text-xs text-ghost">
                    No holdings match your filters.
                  </td>
                </tr>
              ) : groups.map(group => {
                const showGroupHeader = showEntityCol || groupMode === 'pan';

                const byClass: Record<string, MFHoldingRow[]> = Object.fromEntries(
                  ASSET_CLASS_ORDER.map(cls => [
                    cls,
                    sortRows(group.rows.filter(h => h.asset_class === cls), sortKey, sortDir),
                  ])
                );
                const activeClasses = ASSET_CLASS_ORDER.filter(cls => (byClass[cls]?.length ?? 0) > 0);

                return (
                  <Fragment key={group.key}>
                    {showGroupHeader && (
                      <GroupHeader label={group.label} rows={group.rows} colCount={colCount} />
                    )}
                    {activeClasses.map(cls => (
                      <Fragment key={cls}>
                        <AssetClassHeader cls={cls} rows={byClass[cls]} colCount={colCount} />
                        {byClass[cls].map(h => (
                          <DataRow
                            key={h.id}
                            h={h}
                            srNo={++srCounter}
                            showEntityCol={showEntityCol && groupMode === 'entity'}
                            showPanCol={showPanCol}
                          />
                        ))}
                      </Fragment>
                    ))}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}

      </div>
    </div>
  );
}
