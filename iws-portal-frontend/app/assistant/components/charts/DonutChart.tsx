// Donut chart for allocation / mix. SVG arcs, legend with value + percent.
import { color, fmtValue } from './util';

type Item = { label: string; value: number };

function arc(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const p = (a: number) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = p(a0);
  const [x1, y1] = p(a1);
  const large = a1 - a0 > Math.PI ? 1 : 0;
  return `M ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`;
}

export default function DonutChart({ series, unit }: { series: Item[]; unit?: string }) {
  const items = series.filter(s => s.value > 0);
  const total = items.reduce((s, i) => s + i.value, 0);
  if (total <= 0) return null;

  const size = 160, r = 64, cx = size / 2, cy = size / 2;
  let angle = -Math.PI / 2;
  const segs = items.map((it, i) => {
    const frac = it.value / total;
    const a0 = angle;
    const a1 = angle + frac * Math.PI * 2;
    angle = a1;
    return { ...it, i, a0, a1, pct: frac * 100 };
  });

  return (
    <div className="flex flex-wrap items-center gap-5">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Donut chart">
        {segs.map(s => (
          <path
            key={s.label}
            d={arc(cx, cy, r, s.a0, Math.min(s.a1, s.a0 + Math.PI * 2 - 0.001))}
            fill="none"
            stroke={color(s.i)}
            strokeWidth={18}
          />
        ))}
      </svg>
      <ul className="flex-1 min-w-[140px] space-y-1.5">
        {segs.map(s => (
          <li key={s.label} className="flex items-center gap-2 text-xs">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ background: color(s.i) }} />
            <span className="flex-1 truncate text-dim">{s.label}</span>
            <span className="tabular-nums text-ink">{fmtValue(s.value, unit)}</span>
            <span className="w-12 text-right tabular-nums text-ghost">{s.pct.toFixed(1)}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
