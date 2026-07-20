'use client';
// Market data rail for the Overview — commodities, rates, currencies and crypto,
// each under its own heading rather than one undifferentiated list.
//
// Fed by /api/v1/benchmarks, the same endpoint the top ticker uses. That endpoint
// derives week% and YTD% from the stored market_benchmark history, so both are
// real from the first render (see workers/market_history_backfill).
//
// Responsive: a right-hand rail on wide screens, and it falls below the content
// on anything narrower — the dashboard grid does that part.
import { useEffect, useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

export interface Benchmark {
  code: string;
  label: string;
  unit: string;
  current: number | null;
  week_pct: number | null;
  ytd_pct: number | null;
  as_of: string | null;   // date of the current reading; matters for monthly series
}

// Section -> codes, in the order they should read. Codes absent from the API are
// skipped, so a feed that isn't live yet simply doesn't render a row (rather than
// showing a blank one).
const SECTIONS: { title: string; codes: string[]; note?: string }[] = [
  { title: 'Commodities', codes: ['GOLD', 'SILVER', 'PLATINUM', 'COPPER', 'CRUDE_WTI', 'CRUDE_BRENT', 'NATGAS'] },
  { title: 'Rates & Yields', codes: ['US13W', 'US5Y', 'US10Y', 'US30Y', 'GS2030_YTM', 'GS2032_YTM'] },
  { title: 'Currencies', codes: ['USDINR', 'EURINR', 'GBPINR', 'JPYINR', 'AEDINR', 'SGDINR', 'CHFINR', 'EURUSD', 'DXY'] },
  { title: 'Crypto', codes: ['BTCINR', 'BTCUSD', 'ETHINR', 'ETHUSD'] },
  // Monthly, from the IMF. The week/YTD columns are meaningless on a monthly
  // series (a month-old reading hasn't "moved this week"), so this section shows
  // the reading and its as-of month instead — see `PERIODIC` handling below.
  {
    title: 'Inflation',
    codes: ['IN_CPI_YOY', 'IN_WPI_YOY', 'US_CPI_YOY', 'IN_CPI', 'IN_WPI', 'US_CPI'],
    note: 'Monthly · IMF',
  },
  // US only. No free API publishes average Indian FD or home-loan rates, so those
  // rows are absent rather than guessed; they're slated for manual entry.
  {
    title: 'Loans & Deposits',
    codes: ['US_MORTGAGE30', 'US_FD_12M'],
    note: 'FRED',
  },
];

// Codes whose underlying series isn't daily — monthly inflation, weekly mortgage.
// These render their as-of date in place of week/YTD, which would otherwise imply
// a reading ticks day to day when it doesn't.
// Monthly series get a month stamp ("May 2026"); the weekly mortgage average gets
// a day, since naming its month would hide which week's print it is.
const MONTHLY = new Set([
  'IN_CPI', 'IN_CPI_YOY', 'IN_WPI', 'IN_WPI_YOY', 'US_CPI', 'US_CPI_YOY', 'US_FD_12M',
]);
const PERIODIC = new Set([...MONTHLY, 'US_MORTGAGE30']);

// Shorter than the API's label, which is verbose enough for a report but too long
// for a 320px rail.
const SHORT: Record<string, string> = {
  GOLD: 'Gold', SILVER: 'Silver', PLATINUM: 'Platinum', COPPER: 'Copper',
  CRUDE_WTI: 'Crude (WTI)', CRUDE_BRENT: 'Crude (Brent)', NATGAS: 'Natural Gas',
  US13W: 'US 13-Week', US5Y: 'US 5-Year', US10Y: 'US 10-Year', US30Y: 'US 30-Year',
  GS2030_YTM: 'India GS 2030', GS2032_YTM: 'India GS 2032',
  USDINR: 'USD / INR', EURINR: 'EUR / INR', GBPINR: 'GBP / INR', JPYINR: 'JPY / INR',
  AEDINR: 'AED / INR', SGDINR: 'SGD / INR', CHFINR: 'CHF / INR', EURUSD: 'EUR / USD',
  DXY: 'Dollar Index',
  BTCINR: 'Bitcoin ₹', BTCUSD: 'Bitcoin $', ETHINR: 'Ethereum ₹', ETHUSD: 'Ethereum $',
  IN_CPI_YOY: 'India CPI y/y', IN_WPI_YOY: 'India WPI y/y', US_CPI_YOY: 'US CPI y/y',
  IN_CPI: 'India CPI index', IN_WPI: 'India WPI index', US_CPI: 'US CPI index',
  US_MORTGAGE30: 'US home loan 30y', US_FD_12M: 'US FD 12-mo (CD)',
};

function asOfLabel(iso: string | null, monthly: boolean): string {
  if (!iso) return '—';
  const d = new Date(iso + 'T00:00:00');
  return monthly
    ? d.toLocaleDateString('en-IN', { month: 'short', year: 'numeric' })
    : d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

function fmtValue(v: number | null, unit: string): string {
  if (v == null) return '—';
  if (unit === 'pct_raw' || unit === 'pct') return `${v.toFixed(2)}%`;
  if (unit === 'fx') return v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 4 });
  // Crypto in INR runs to millions — compact it so the column doesn't blow out.
  if (Math.abs(v) >= 100_000) {
    return v.toLocaleString('en-IN', { notation: 'compact', maximumFractionDigits: 2 });
  }
  return v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function Pct({ v }: { v: number | null }) {
  if (v == null) return <span className="text-ghost">—</span>;
  const up = v >= 0;
  return (
    <span style={{ color: up ? 'var(--gain)' : 'var(--peril)' }}>
      {up ? '▲' : '▼'} {Math.abs(v * 100).toFixed(2)}%
    </span>
  );
}

export default function MarketRail() {
  const [rows, setRows] = useState<Benchmark[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${API_URL}/api/v1/benchmarks`, { credentials: 'include' });
        if (!res.ok) { if (alive) setRows([]); return; }
        const d = await res.json();
        if (alive) setRows(d);
      } catch { if (alive) setRows([]); }
    };
    load();
    const id = setInterval(load, 60_000);   // same cadence as the ticker
    return () => { alive = false; clearInterval(id); };
  }, []);

  if (!rows || rows.length === 0) return null;
  const byCode = new Map(rows.map(r => [r.code, r]));

  const sections = SECTIONS
    .map(s => {
      const rows = s.codes.map(c => byCode.get(c)).filter((r): r is Benchmark => !!r && r.current != null);
      return {
        title: s.title,
        note: s.note,
        rows,
        // Inflation and Loans & Deposits are wholly periodic, so their last two
        // columns carry the period the reading is from rather than a week/YTD
        // move. Derived from the rows actually present, not hardcoded: a section
        // that gains a daily series picks up the Wk/YTD headers on its own.
        allPeriodic: rows.length > 0 && rows.every(r => PERIODIC.has(r.code)),
      };
    })
    .filter(s => s.rows.length > 0);
  if (sections.length === 0) return null;

  return (
    <aside className="bg-card rounded-lg border border-rule overflow-hidden" aria-label="Market data">
      <div className="px-4 py-3 border-b border-rule">
        <h2 className="text-sm font-semibold text-ink">Markets</h2>
      </div>

      {sections.map(sec => (
        <section key={sec.title}>
          <h3 className="px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wide text-ghost bg-page border-y border-rule flex items-baseline justify-between">
            <span>{sec.title}</span>
            {sec.note && <span className="font-normal normal-case tracking-normal">{sec.note}</span>}
          </h3>
          <table className="w-full text-xs" style={{ fontVariantNumeric: 'tabular-nums' }}>
            {/* Column headers per segment rather than once for the whole rail —
                the last two columns mean different things in different sections. */}
            <thead>
              <tr className="text-[9px] uppercase tracking-wide text-ghost">
                <th scope="col" className="px-4 pt-2 pb-1 text-left font-normal">
                  <span className="sr-only">Series</span>
                </th>
                <th scope="col" className="px-2 pt-2 pb-1 text-right font-normal">
                  <span className="sr-only">Latest</span>
                </th>
                {sec.allPeriodic ? (
                  // These columns carry the period the reading is from, not a move.
                  // Caveat: Loans & Deposits mixes cadence — US_FD_12M is monthly but
                  // US_MORTGAGE30 is a weekly print — so "Monthly" is loose there. The
                  // row's own stamp is a day rather than a month, which shows which.
                  <th scope="col" colSpan={2} className="px-4 pt-2 pb-1 text-right font-normal">Monthly</th>
                ) : (
                  <>
                    <th scope="col" className="px-2 pt-2 pb-1 text-right font-normal">Wk</th>
                    <th scope="col" className="px-4 pt-2 pb-1 text-right font-normal">YTD</th>
                  </>
                )}
              </tr>
            </thead>
            <tbody>
              {sec.rows.map(r => (
                <tr key={r.code} className="border-b border-rule/50 last:border-0">
                  <td className="px-4 py-2 text-dim whitespace-nowrap">{SHORT[r.code] ?? r.label}</td>
                  <td className="px-2 py-2 text-right font-medium text-ink whitespace-nowrap">
                    {fmtValue(r.current, r.unit)}
                  </td>
                  {PERIODIC.has(r.code) ? (
                    // A monthly/weekly reading has no meaningful week/YTD move — showing
                    // one would imply it ticks. Label the period it's from instead.
                    <td className="px-2 py-2 pr-4 text-right text-ghost whitespace-nowrap" colSpan={2}>
                      {asOfLabel(r.as_of, MONTHLY.has(r.code))}
                    </td>
                  ) : (
                    <>
                      <td className="px-2 py-2 text-right whitespace-nowrap"><Pct v={r.week_pct} /></td>
                      <td className="px-4 py-2 text-right whitespace-nowrap"><Pct v={r.ytd_pct} /></td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}

      <p className="px-4 py-2 text-[10px] text-ghost border-t border-rule">
        YTD measured from 31-Mar. Live via Yahoo Finance; inflation from the IMF, US rates from FRED.
      </p>
    </aside>
  );
}
