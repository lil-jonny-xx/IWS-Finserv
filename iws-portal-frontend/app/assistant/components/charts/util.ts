// Shared chart helpers — palette pulled from the app's oklch design tokens.

export const PALETTE = [
  'var(--chart-equity)',
  'var(--chart-fixed)',
  'var(--chart-alt)',
  'var(--chart-hybrid)',
  'var(--chart-arbitrage)',
  'var(--prime)',
];

export function color(i: number): string {
  return PALETTE[i % PALETTE.length];
}

// Format a value for labels/tooltips. Compact Indian notation for rupee amounts.
export function fmtValue(v: number, unit?: string): string {
  if (unit === '%') return `${v.toFixed(2)}%`;
  const abs = Math.abs(v);
  let s: string;
  if (abs >= 1e7) s = `${(v / 1e7).toFixed(2)} Cr`;
  else if (abs >= 1e5) s = `${(v / 1e5).toFixed(2)} L`;
  else s = Math.round(v).toLocaleString('en-IN');
  return unit && unit !== '%' ? `${unit}${s}` : s;
}

// Diverging color for a correlation cell in [-1, 1]: peril (neg) → ghost (0) → gain (pos).
export function corrColor(v: number | null): string {
  if (v == null || Number.isNaN(v)) return 'var(--rule)';
  const t = Math.max(-1, Math.min(1, v));
  const a = Math.min(1, Math.abs(t)) * 0.85 + 0.08;
  const base = t >= 0 ? 'var(--gain)' : 'var(--peril)';
  return `color-mix(in oklch, ${base} ${Math.round(a * 100)}%, var(--card))`;
}
