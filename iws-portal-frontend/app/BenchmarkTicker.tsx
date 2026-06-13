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

const SHORT: Record<string, string> = {
  NIFTY: 'NIFTY', SENSEX: 'SENSEX',
  GS2032_YTM: 'GS 2032', GS2030_YTM: 'GS 2030',
  GS2032_PRICE: 'GS 2032 ₹', GS2030_PRICE: 'GS 2030 ₹',
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

  // Hide entirely when unauthenticated or no data (e.g. the login screen).
  if (!rows || rows.length === 0) return null;
  const shown = rows.filter(r => r.current != null);
  if (shown.length === 0) return null;

  return (
    <div
      style={{
        position: 'sticky', top: 0, zIndex: 60,
        background: 'var(--ink)', color: 'var(--card)',
        borderBottom: '1px solid var(--rule)',
      }}
      className="w-full overflow-x-auto"
      aria-label="Market benchmarks"
    >
      <div className="flex items-center gap-6 px-4 py-1.5 text-xs whitespace-nowrap"
           style={{ fontVariantNumeric: 'tabular-nums' }}>
        <span className="font-bold tracking-wide" style={{ opacity: 0.6 }}>MARKETS</span>
        {shown.map(r => {
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
        <span style={{ opacity: 0.4 }} className="ml-auto pl-4">wk %</span>
      </div>
    </div>
  );
}
