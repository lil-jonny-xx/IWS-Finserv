// Line chart for a value / drawdown path over time. Compact SVG with a soft area fill.
import { fmtValue } from './util';

type Point = { x: string; y: number };

export default function LineChart({ points, unit }: { points: Point[]; unit?: string }) {
  if (points.length < 2) return null;

  const w = 480, h = 140, padX = 8, padY = 12;
  const ys = points.map(p => p.y);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const span = maxY - minY || 1;

  const sx = (i: number) => padX + (i / (points.length - 1)) * (w - padX * 2);
  const sy = (y: number) => padY + (1 - (y - minY) / span) * (h - padY * 2);

  const line = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${sx(i).toFixed(1)} ${sy(p.y).toFixed(1)}`).join(' ');
  const area = `${line} L ${sx(points.length - 1).toFixed(1)} ${h - padY} L ${sx(0).toFixed(1)} ${h - padY} Z`;
  const rising = points[points.length - 1].y >= points[0].y;
  const stroke = rising ? 'var(--gain)' : 'var(--peril)';
  const gid = `area-${rising ? 'up' : 'dn'}`;

  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full" role="img" aria-label="Line chart" preserveAspectRatio="none">
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.18" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        <path d={area} fill={`url(#${gid})`} />
        <path d={line} fill="none" stroke={stroke} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-ghost">
        <span>{points[0].x} · {fmtValue(points[0].y, unit)}</span>
        <span>{points[points.length - 1].x} · {fmtValue(points[points.length - 1].y, unit)}</span>
      </div>
    </div>
  );
}
