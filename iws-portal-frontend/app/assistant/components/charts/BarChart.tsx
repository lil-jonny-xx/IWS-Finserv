// Horizontal bar chart. Handles negatives (laggards / weekly change) with a zero axis;
// all-positive series (allocation) use the categorical palette.
import { color, fmtValue } from './util';

type Item = { label: string; value: number };

export default function BarChart({ series, unit }: { series: Item[]; unit?: string }) {
  if (!series.length) return null;

  const hasNeg = series.some(s => s.value < 0);
  const max = Math.max(...series.map(s => Math.abs(s.value)), 1e-9);
  // Zero line position: centered if there are negatives, else left.
  const zero = hasNeg ? 50 : 0;

  return (
    <ul className="space-y-2">
      {series.map((s, i) => {
        const frac = Math.abs(s.value) / max;             // 0..1
        const widthPct = frac * (hasNeg ? 50 : 100);
        const fill = hasNeg
          ? (s.value >= 0 ? 'var(--gain)' : 'var(--peril)')
          : color(i);
        return (
          <li key={s.label} className="text-xs">
            <div className="mb-0.5 flex justify-between gap-2">
              <span className="truncate text-dim">{s.label}</span>
              <span
                className="shrink-0 tabular-nums"
                style={{ color: hasNeg ? fill : 'var(--ink)' }}
              >
                {fmtValue(s.value, unit)}
              </span>
            </div>
            <div className="relative h-2.5 rounded bg-page">
              <div
                className="absolute top-0 h-full rounded"
                style={{
                  background: fill,
                  width: `${widthPct}%`,
                  left: s.value >= 0 ? `${zero}%` : `${zero - widthPct}%`,
                }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
