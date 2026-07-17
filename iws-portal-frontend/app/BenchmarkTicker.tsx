'use client';
import { useEffect, useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface Benchmark {
  code: string;
  label: string;
  unit: string;
  current: number | null;
  week_pct: number | null;
}

// The ticker carries INDICES only — the world's markets across the top. Everything
// else (commodities, rates, FX, crypto) lives in the Overview's Markets rail, where
// it can be grouped under headings instead of scrolling past as one long line.
//
// Grouped by region and rendered in this order, with a separator between groups, so
// the strip reads as India | US | rest-of-world rather than an undifferentiated run.
const TICKER_GROUPS: { region: string; codes: string[] }[] = [
  { region: 'INDIA', codes: ['NIFTY', 'SENSEX', 'NIFTYBANK'] },
  { region: 'US',    codes: ['DOWJONES', 'NASDAQ', 'SP500', 'RUSSELL2000', 'VIX'] },
  { region: 'WORLD', codes: ['FTSE100', 'DAX', 'CAC40', 'STOXX50', 'NIKKEI', 'HANGSENG',
                             'SHANGHAI', 'KOSPI', 'ASX200', 'TSX', 'BOVESPA'] },
];

const SHORT: Record<string, string> = {
  NIFTY: 'NIFTY', SENSEX: 'SENSEX', NIFTYBANK: 'BANK NIFTY',
  DOWJONES: 'DOW', NASDAQ: 'NASDAQ', SP500: 'S&P 500', RUSSELL2000: 'RUSSELL', VIX: 'VIX',
  FTSE100: 'FTSE', DAX: 'DAX', CAC40: 'CAC', STOXX50: 'STOXX 50', NIKKEI: 'NIKKEI',
  HANGSENG: 'HANG SENG', SHANGHAI: 'SHANGHAI', KOSPI: 'KOSPI', ASX200: 'ASX',
  TSX: 'TSX', BOVESPA: 'BOVESPA',
};

function fmt(v: number | null, unit: string): string {
  if (v == null) return '—';
  if (unit === 'pct') return `${(v * 100).toFixed(2)}%`;
  return v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function BenchmarkTicker() {
  const [rows, setRows] = useState<Benchmark[] | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const res = await fetch(`${API_URL}/api/v1/benchmarks`, { credentials: 'include' });
        if (!res.ok) { if (alive) setRows([]); return; }   // 401 on login → hide
        const data = await res.json();
        if (alive) setRows(data);
      } catch { if (alive) setRows([]); }
    };
    load();
    const id = setInterval(load, 60_000);   // live-refresh every minute
    return () => { alive = false; clearInterval(id); };
  }, []);

  // Renders the strip only. The sticky bar, background and the sign-out beside it
  // live in TopBar, so the sign-out stays put on every page even when the
  // benchmarks are empty or the feed is down.
  if (!rows || rows.length === 0) return null;
  const byCode = new Map(rows.filter(r => r.current != null).map(r => [r.code, r]));
  const groups = TICKER_GROUPS
    .map(g => ({ region: g.region, rows: g.codes.map(c => byCode.get(c)).filter((r): r is Benchmark => !!r) }))
    .filter(g => g.rows.length > 0);
  if (groups.length === 0) return null;

  return (
    <div className="flex items-center gap-4 px-4 py-1.5 text-xs whitespace-nowrap overflow-x-auto nav-scroll"
         style={{ fontVariantNumeric: 'tabular-nums' }}
         aria-label="Market indices">
      {groups.map((g, gi) => (
        <span key={g.region} className="inline-flex items-center gap-4">
          {gi > 0 && <span aria-hidden style={{ opacity: 0.25 }}>|</span>}
          <span className="font-bold tracking-wide" style={{ opacity: 0.5 }}>{g.region}</span>
          {g.rows.map(r => {
            const up = (r.week_pct ?? 0) >= 0;
            return (
              <span key={r.code} className="inline-flex items-center gap-1.5">
                <span style={{ opacity: 0.7 }}>{SHORT[r.code] ?? r.label}</span>
                <span className="font-semibold">{fmt(r.current, r.unit)}</span>
                {r.week_pct != null && (
                  <span style={{ color: up ? '#22c55e' : '#ef4444' }}>
                    {up ? '▲' : '▼'} {Math.abs(r.week_pct * 100).toFixed(2)}%
                  </span>
                )}
              </span>
            );
          })}
        </span>
      ))}
      <span style={{ opacity: 0.4 }} className="pl-2">wk %</span>
    </div>
  );
}
