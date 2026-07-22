'use client';
import { useEffect, useState, useMemo, Fragment } from 'react';
import { useRouter } from 'next/navigation';
import { Glass } from '@/app/components/PrivacyGlass';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface RealisedRow {
  entity: string;
  category: string;
  group: string;
  security_name: string;
  purchase_amount: number | null;
  sale_date: string;
  sale_amount: number | null;
  pnl: number | null;
  st_pnl: number | null;   // Indian equity only: short-term slice (held ≤ 12 months)
  lt_pnl: number | null;   // Indian equity only: long-term slice (held > 12 months)
  return_pct: number | null;
}

// Section order on the page; any category not listed falls in after these, sorted.
const CATEGORY_ORDER = ['Equity', 'Mutual Funds', 'Foreign Equity', 'PMS'];

function inr(v: number | null): string {
  if (v == null) return '—';
  return v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
function pct(v: number | null): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(2)}%`;
}

// Indian financial year label for a sale date: FY runs Apr–Mar, so a sale on
// 2026-06-19 belongs to FY 2026-27 and one on 2026-02-18 to FY 2025-26. Bucketing
// on the calendar year instead would split every FY across two rows and make the
// year-on-year view disagree with the tax view of the same trades.
function fyOf(saleDate: string): string {
  if (!saleDate) return '—';
  const [y, m] = saleDate.split('-').map(Number);
  if (!y || !m) return '—';
  const start = m >= 4 ? y : y - 1;
  return `${start}-${String(start + 1).slice(-2)}`;
}

function pnlStyle(v: number | null) {
  if (v == null) return { color: 'var(--ghost)' };
  return { color: v >= 0 ? 'var(--gain)' : 'var(--peril)' };
}

function Toggle<T extends string>({ label, value, onChange, options }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { v: T; label: string }[];
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium" style={{ color: 'var(--ghost)' }}>{label}</span>
      <div className="inline-flex rounded-lg overflow-hidden" style={{ border: '1px solid var(--rule)' }}>
        {options.map(o => {
          const active = o.v === value;
          return (
            <button
              key={o.v}
              onClick={() => onChange(o.v)}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={{
                background: active ? 'var(--prime)' : 'var(--card)',
                color: active ? '#fff' : 'var(--dim)',
              }}
            >{o.label}</button>
          );
        })}
      </div>
    </div>
  );
}

type Period = 'fy' | 'inception';
type Switches = 'include' | 'exclude';
type View = 'detail' | 'entity' | 'yoy' | 'dividends';

interface DividendRow {
  entity: string;
  security_name: string;
  ex_date: string;
  quantity: number;
  rate_per_share: number;
  amount: number;
  fy: string;
  variance_pct: number | null;   // set once validated against a broker report
  feed: string | null;
}
interface DividendCoverage {
  resolved: number;
  unresolved: number;
  unresolved_symbols: string[];
}

export default function RealisedGainsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<RealisedRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState<Period>('fy');
  const [switches, setSwitches] = useState<Switches>('include');
  const [view, setView] = useState<View>('detail');

  const [divRows, setDivRows] = useState<DividendRow[]>([]);
  const [divCov, setDivCov] = useState<DividendCoverage | null>(null);

  // Year-on-year is meaningless against a single FY, so that view always pulls the
  // whole history regardless of the Period toggle (which is hidden while it's active).
  const effectivePeriod: Period = view === 'yoy' ? 'inception' : period;

  // Role only decides which nav tabs show (Manual Data is admin-only); the report
  // data itself is available to every authenticated user.
  useEffect(() => {
    const c = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: c.signal })
      .then(r => r.ok ? r.json() : null)
      .then((u: { role?: string } | null) => { if (!u) router.replace('/'); })
      .catch(err => { if (err.name !== 'AbortError') router.replace('/'); });
    return () => c.abort();
  }, [router]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const res = await fetch(
        `${API_URL}/api/v1/realised-gains?period=${effectivePeriod}&switches=${switches}`,
        { credentials: 'include' },
      );
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) setRows(await res.json());
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [router, effectivePeriod, switches]);

  // Dividends are a separate feed on the same page — fetched only when that view is
  // opened, and only once, since the figures are recomputed by a weekly worker rather
  // than changing with the Period/Switches toggles.
  useEffect(() => {
    if (view !== 'dividends' || divCov !== null) return;
    let cancelled = false;
    (async () => {
      const res = await fetch(`${API_URL}/api/v1/dividends?period=inception`,
                              { credentials: 'include' });
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) {
        const j = await res.json();
        setDivRows(j.rows ?? []);
        setDivCov(j.coverage ?? { resolved: 0, unresolved: 0, unresolved_symbols: [] });
      }
    })();
    return () => { cancelled = true; };
  }, [view, divCov, router]);

  // Show the loading state while a toggle change refetches.
  const changePeriod = (p: Period) => { setLoading(true); setPeriod(p); };
  const changeSwitches = (s: Switches) => { setLoading(true); setSwitches(s); };

  const totalPnl = rows.reduce((s, r) => s + (r.pnl ?? 0), 0);
  // Short/long-term totals only exist for Indian equity (FIFO-split); other
  // categories carry null, so they don't contribute.
  const totalSt = rows.reduce((s, r) => s + (r.st_pnl ?? 0), 0);
  const totalLt = rows.reduce((s, r) => s + (r.lt_pnl ?? 0), 0);
  const hasStLt = rows.some(r => r.st_pnl != null || r.lt_pnl != null);

  // Split rows into asset sections (Equity / Mutual Funds / …). In "since inception"
  // mode each section is ordered latest sale first; FY-to-date keeps the backend's
  // chronological order.
  const sections = useMemo(() => {
    const map = new Map<string, RealisedRow[]>();
    for (const r of rows) {
      const c = r.category || r.group || 'Other';
      if (!map.has(c)) map.set(c, []);
      map.get(c)!.push(r);
    }
    const ordered = [
      ...CATEGORY_ORDER.filter(c => map.has(c)),
      ...[...map.keys()].filter(c => !CATEGORY_ORDER.includes(c)).sort(),
    ];
    return ordered.map(cat => {
      let rs = map.get(cat)!;
      if (period === 'inception') {
        rs = [...rs].sort((a, b) => b.sale_date.localeCompare(a.sale_date));
      }
      return { cat, rows: rs };
    });
  }, [rows, period]);

  // Per-entity totals. The API already returns `entity` on every row (visibility is
  // uniform), so this is a pure regroup of the same data the detail table shows —
  // no extra request, and it always agrees with the headline total.
  const byEntity = useMemo(() => {
    const m = new Map<string, { pnl: number; st: number; lt: number; n: number; hasStLt: boolean }>();
    for (const r of rows) {
      const k = r.entity || '—';
      const cur = m.get(k) ?? { pnl: 0, st: 0, lt: 0, n: 0, hasStLt: false };
      cur.pnl += r.pnl ?? 0;
      cur.st  += r.st_pnl ?? 0;
      cur.lt  += r.lt_pnl ?? 0;
      cur.n   += 1;
      // ST/LT exist only for Indian equity; an entity holding only MF/PMS shows a
      // dash rather than a misleading ₹0.
      if (r.st_pnl != null || r.lt_pnl != null) cur.hasStLt = true;
      m.set(k, cur);
    }
    return [...m.entries()].sort((a, b) => b[1].pnl - a[1].pnl);
  }, [rows]);

  // FY × entity matrix for the year-on-year view.
  const yoy = useMemo(() => {
    const ents = [...new Set(rows.map(r => r.entity || '—'))].sort();
    const grid = new Map<string, Map<string, number>>();
    for (const r of rows) {
      const fy = fyOf(r.sale_date);
      if (fy === '—') continue;            // a row with no sale date can't be dated
      if (!grid.has(fy)) grid.set(fy, new Map());
      const em = grid.get(fy)!;
      const k = r.entity || '—';
      em.set(k, (em.get(k) ?? 0) + (r.pnl ?? 0));
    }
    const fys = [...grid.keys()].sort().reverse();   // most recent FY first
    const cell = (fy: string, e: string) => grid.get(fy)?.get(e) ?? 0;
    const rowTotal = (fy: string) =>
      [...(grid.get(fy)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (e: string) => fys.reduce((s, fy) => s + cell(fy, e), 0);
    return { ents, fys, cell, rowTotal, colTotal };
  }, [rows]);

  // Dividends as an FY × entity matrix — the same shape as the realised YoY view, so
  // the two read the same way. The backend already stamps each row with its Indian FY.
  const divYoy = useMemo(() => {
    const ents = [...new Set(divRows.map(r => r.entity || '—'))].sort();
    const grid = new Map<string, Map<string, number>>();
    for (const r of divRows) {
      if (!grid.has(r.fy)) grid.set(r.fy, new Map());
      const em = grid.get(r.fy)!;
      const k = r.entity || '—';
      em.set(k, (em.get(k) ?? 0) + r.amount);
    }
    const fys = [...grid.keys()].sort().reverse();
    const cell = (fy: string, e: string) => grid.get(fy)?.get(e) ?? 0;
    const rowTotal = (fy: string) => [...(grid.get(fy)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (e: string) => fys.reduce((s, fy) => s + cell(fy, e), 0);
    const total = divRows.reduce((s, r) => s + r.amount, 0);
    return { ents, fys, cell, rowTotal, colTotal, total };
  }, [divRows]);

  return (
    <div className="min-h-screen" style={{ background: 'var(--page)' }}>
      {/* Section tabs are global — see components/GlobalNav in the root layout. */}

      <main className="shell py-6">
        <div className="mb-4 flex items-end justify-between">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>
              {view === 'dividends' ? 'Dividends (all years)' : `Realised Gains (${
                view === 'yoy' ? 'year on year'
                  : effectivePeriod === 'inception' ? 'since inception' : 'FY to date'})`}
            </h1>
            {view === 'dividends' ? (
              <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                Indian dividends are paid straight to your bank, never through the broker, so these are
                <strong> derived</strong>: ex-date and rate per share from market data, multiplied by the
                quantity the trade ledger says was held on that date. Figures are <strong>gross</strong> —
                dividends over ₹5,000 a year attract 10% TDS, so the amount credited is lower.
              </p>
            ) : (
              <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                MF realised auto-computed from CAS transactions. Indian equity is FIFO-matched (broker/tax
                basis), gross of charges, split into short-term (held ≤ 12 months) and long-term (&gt; 12 months).
                {' '}Switches are {switches === 'exclude' ? 'excluded' : 'included'}.
              </p>
            )}
          </div>
          {/* Narrow block, so the pane gets a little breathing room around the
              figures rather than clamping to the text. */}
          {view === 'dividends' ? (
            <Glass label="Dividends" className="shrink-0">
              <div className="text-right px-2 py-1">
                <div className="text-xs" style={{ color: 'var(--ghost)' }}>Total dividends (gross)</div>
                <div className="text-base font-bold" style={{ color: 'var(--gain)' }}>₹{inr(divYoy.total)}</div>
                <div className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                  {divRows.length} payment{divRows.length === 1 ? '' : 's'}
                </div>
              </div>
            </Glass>
          ) : (
          <Glass label="P&amp;L" className="shrink-0">
          <div className="text-right px-2 py-1">
            <div className="text-xs" style={{ color: 'var(--ghost)' }}>Total realised P&amp;L</div>
            <div className="text-base font-bold" style={{ color: totalPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              ₹{inr(totalPnl)}
            </div>
            {hasStLt && (
              <div className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                <span style={{ color: totalSt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>ST ₹{inr(totalSt)}</span>
                {' · '}
                <span style={{ color: totalLt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>LT ₹{inr(totalLt)}</span>
              </div>
            )}
          </div>
          </Glass>
          )}
        </div>

        <div className="mb-6 flex flex-wrap items-center gap-x-6 gap-y-2">
          <Toggle<View>
            label="View"
            value={view}
            // Only flip to Loading when the switch actually changes what gets fetched.
            // Detail / By entity / Dividends all reuse data already in state, so
            // setting it unconditionally left the page stuck on "Loading…" — the
            // effect below never re-ran to clear it.
            onChange={(v) => {
              const nextPeriod: Period = v === 'yoy' ? 'inception' : period;
              if (nextPeriod !== effectivePeriod) setLoading(true);
              setView(v);
            }}
            options={[
              { v: 'detail',    label: 'Detail' },
              { v: 'entity',    label: 'By entity' },
              { v: 'yoy',       label: 'Year on year' },
              { v: 'dividends', label: 'Dividends' },
            ]}
          />
          {/* Hidden rather than disabled in the YoY view: that view always spans the
              whole history, so offering a period control that does nothing would be
              worse than not offering one. */}
          {view !== 'yoy' && view !== 'dividends' && (
            <Toggle<Period>
              label="Period"
              value={period}
              onChange={changePeriod}
              options={[{ v: 'fy', label: 'FY to date' }, { v: 'inception', label: 'Since inception' }]}
            />
          )}
          {view !== 'dividends' && (
          <Toggle<Switches>
            label="Switches"
            value={switches}
            onChange={changeSwitches}
            options={[{ v: 'include', label: 'Include' }, { v: 'exclude', label: 'Exclude' }]}
          />
          )}
        </div>

        {view === 'dividends' ? (
          divCov === null ? (
            <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
          ) : (
          <>
            {/* Coverage is stated up front, not buried. A scrip with no market-data
                ticker contributes zero, so without this line an under-count would be
                indistinguishable from a genuinely dividend-free portfolio. */}
            {divCov.unresolved > 0 && (
              <div className="mb-4 px-3 py-2 rounded-lg text-xs"
                   style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                Matched {divCov.resolved} of {divCov.resolved + divCov.unresolved} securities to a market-data
                feed. {divCov.unresolved} could not be matched and contribute nothing — typically SME-board
                scrips, sovereign gold bonds (which pay interest, not dividends) and renamed tickers.
                {divCov.unresolved_symbols.length > 0 && (
                  <span style={{ color: 'var(--ghost)' }}>
                    {' '}Unmatched: {divCov.unresolved_symbols.slice(0, 12).join(', ')}
                    {divCov.unresolved_symbols.length > 12 ? ` +${divCov.unresolved_symbols.length - 12} more` : ''}.
                  </span>
                )}
              </div>
            )}
            {divRows.length === 0 ? (
              <div className="py-16 text-center rounded-xl"
                   style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
                <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No dividends computed yet</p>
                <p className="text-xs" style={{ color: 'var(--ghost)' }}>
                  Run the dividend worker to derive them from the trade ledger.
                </p>
              </div>
            ) : (
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Financial year</th>
                      {divYoy.ents.map(e => (
                        <th key={e} className="px-3 py-2 text-right font-semibold whitespace-nowrap">{e}</th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {divYoy.fys.map(fy => (
                      <tr key={fy} style={{ borderTop: '1px solid var(--rule)' }}>
                        <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>FY {fy}</td>
                        {divYoy.ents.map(e => {
                          const v = divYoy.cell(fy, e);
                          return (
                            <td key={e} className="px-3 py-2 text-right"
                                style={{ color: v === 0 ? 'var(--ghost)' : 'var(--gain)' }}>
                              {v === 0 ? '—' : `₹${inr(v)}`}
                            </td>
                          );
                        })}
                        <td className="px-3 py-2 text-right font-medium" style={{ color: 'var(--gain)' }}>
                          ₹{inr(divYoy.rowTotal(fy))}
                        </td>
                      </tr>
                    ))}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>All years</td>
                      {divYoy.ents.map(e => (
                        <td key={e} className="px-3 py-2 text-right font-bold" style={{ color: 'var(--gain)' }}>
                          ₹{inr(divYoy.colTotal(e))}
                        </td>
                      ))}
                      <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--gain)' }}>
                        ₹{inr(divYoy.total)}
                      </td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            )}
          </>
          )
        ) : loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : rows.length === 0 ? (
          <div className="py-16 text-center rounded-xl" style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
            <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No realised gains this financial year</p>
            <p className="text-xs" style={{ color: 'var(--ghost)' }}>
              They appear as funds/stocks are sold (or after a tradebook import).
            </p>
          </div>
        ) : view === 'entity' ? (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  <th className="px-3 py-2 text-left font-semibold">Entity</th>
                  <th className="px-3 py-2 text-right font-semibold">Sales</th>
                  <th className="px-3 py-2 text-right font-semibold">Realised P&amp;L</th>
                  <th className="px-3 py-2 text-right font-semibold">Short-term</th>
                  <th className="px-3 py-2 text-right font-semibold">Long-term</th>
                  <th className="px-3 py-2 text-right font-semibold">Share</th>
                </tr>
              </thead>
              <tbody>
                {byEntity.map(([ent, v]) => (
                  <tr key={ent} style={{ borderTop: '1px solid var(--rule)' }}>
                    <td className="px-3 py-2 font-medium" style={{ color: 'var(--ink)' }}>{ent}</td>
                    <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>{v.n}</td>
                    <td className="px-3 py-2 text-right font-medium" style={pnlStyle(v.pnl)}>₹{inr(v.pnl)}</td>
                    <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.st : null)}>
                      {v.hasStLt ? `₹${inr(v.st)}` : '—'}</td>
                    <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.lt : null)}>
                      {v.hasStLt ? `₹${inr(v.lt)}` : '—'}</td>
                    {/* Share of the absolute total, so entities that lost money still
                        read as a positive contribution to the spread rather than a
                        negative percentage of a possibly-negative denominator. */}
                    <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>
                      {(() => {
                        const denom = byEntity.reduce((s, [, x]) => s + Math.abs(x.pnl), 0);
                        return denom ? `${((Math.abs(v.pnl) / denom) * 100).toFixed(1)}%` : '—';
                      })()}
                    </td>
                  </tr>
                ))}
                <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                  <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                  <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>{rows.length}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(totalPnl)}>₹{inr(totalPnl)}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(hasStLt ? totalSt : null)}>
                    {hasStLt ? `₹${inr(totalSt)}` : '—'}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(hasStLt ? totalLt : null)}>
                    {hasStLt ? `₹${inr(totalLt)}` : '—'}</td>
                  <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>100%</td>
                </tr>
              </tbody>
            </table>
            </div>
          </div>
        ) : view === 'yoy' ? (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            {/* Entity columns can outgrow a narrow screen — scroll the table, never
                the page (see the responsive rule in the shared layout). */}
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Financial year</th>
                  {yoy.ents.map(e => (
                    <th key={e} className="px-3 py-2 text-right font-semibold whitespace-nowrap">{e}</th>
                  ))}
                  <th className="px-3 py-2 text-right font-semibold">Total</th>
                </tr>
              </thead>
              <tbody>
                {yoy.fys.map(fy => (
                  <tr key={fy} style={{ borderTop: '1px solid var(--rule)' }}>
                    <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>FY {fy}</td>
                    {yoy.ents.map(e => {
                      const v = yoy.cell(fy, e);
                      return (
                        <td key={e} className="px-3 py-2 text-right"
                            style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                          {v === 0 ? '—' : `₹${inr(v)}`}
                        </td>
                      );
                    })}
                    <td className="px-3 py-2 text-right font-medium" style={pnlStyle(yoy.rowTotal(fy))}>
                      ₹{inr(yoy.rowTotal(fy))}
                    </td>
                  </tr>
                ))}
                <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                  <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>All years</td>
                  {yoy.ents.map(e => (
                    <td key={e} className="px-3 py-2 text-right font-bold" style={pnlStyle(yoy.colTotal(e))}>
                      ₹{inr(yoy.colTotal(e))}
                    </td>
                  ))}
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(totalPnl)}>₹{inr(totalPnl)}</td>
                </tr>
              </tbody>
            </table>
            </div>
          </div>
        ) : (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  {['Entity', 'Group', 'Security', 'Purchase Amt', 'Sale Date', 'Sale Amt', 'P&L', 'Short-term', 'Long-term', 'Return %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sections.map(sec => {
                  const secPnl = sec.rows.reduce((s, r) => s + (r.pnl ?? 0), 0);
                  // ST/LT subtotals are meaningful only for sections that carry them
                  // (Indian equity). Elsewhere show a dash instead of a misleading ₹0.
                  const secHasStLt = sec.rows.some(r => r.st_pnl != null || r.lt_pnl != null);
                  const secSt = secHasStLt ? sec.rows.reduce((s, r) => s + (r.st_pnl ?? 0), 0) : null;
                  const secLt = secHasStLt ? sec.rows.reduce((s, r) => s + (r.lt_pnl ?? 0), 0) : null;
                  return (
                    <Fragment key={sec.cat}>
                      <tr style={{ background: 'var(--page)', borderTop: '1px solid var(--rule)' }}>
                        <td colSpan={6} className="px-3 py-1.5 font-semibold" style={{ color: 'var(--ink)' }}>
                          {sec.cat}
                          <span className="ml-2 font-normal" style={{ color: 'var(--ghost)' }}>({sec.rows.length})</span>
                        </td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>₹{inr(secPnl)}</td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secSt == null ? 'var(--ghost)' : secSt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                          {secSt == null ? '—' : `₹${inr(secSt)}`}</td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secLt == null ? 'var(--ghost)' : secLt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                          {secLt == null ? '—' : `₹${inr(secLt)}`}</td>
                        <td className="px-3 py-1.5" />
                      </tr>
                      {sec.rows.map((r, i) => (
                        <tr key={`${sec.cat}-${i}`} style={{ borderTop: '1px solid var(--rule)' }}>
                          <td className="px-3 py-2" style={{ color: 'var(--dim)' }}>{r.entity}</td>
                          <td className="px-3 py-2" style={{ color: 'var(--ghost)' }}>{r.group}</td>
                          <td className="px-3 py-2" style={{ color: 'var(--ink)' }}>{r.security_name}</td>
                          <td className="px-3 py-2 text-right">{inr(r.purchase_amount)}</td>
                          <td className="px-3 py-2">{r.sale_date}</td>
                          <td className="px-3 py-2 text-right">{inr(r.sale_amount)}</td>
                          <td className="px-3 py-2 text-right font-medium"
                              style={{ color: (r.pnl ?? 0) >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.pnl)}</td>
                          <td className="px-3 py-2 text-right"
                              style={{ color: r.st_pnl == null ? 'var(--ghost)' : r.st_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.st_pnl)}</td>
                          <td className="px-3 py-2 text-right"
                              style={{ color: r.lt_pnl == null ? 'var(--ghost)' : r.lt_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.lt_pnl)}</td>
                          <td className="px-3 py-2 text-right">{pct(r.return_pct)}</td>
                        </tr>
                      ))}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
