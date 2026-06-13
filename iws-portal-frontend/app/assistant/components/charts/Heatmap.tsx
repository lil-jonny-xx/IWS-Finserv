// Correlation heatmap. Square grid, diverging color, value labels.
import { corrColor } from './util';

export default function Heatmap({
  labels,
  matrix,
}: {
  labels: string[];
  matrix: (number | null)[][];
}) {
  const n = labels.length;
  if (n < 2 || matrix.length !== n) return null;

  // Short axis tick (first 4 chars) to keep the grid compact.
  const tick = (s: string) => (s.length > 5 ? s.slice(0, 4) + '…' : s);

  return (
    <div className="overflow-x-auto">
      <table className="border-separate" style={{ borderSpacing: 2 }}>
        <thead>
          <tr>
            <th className="p-1" />
            {labels.map(l => (
              <th key={l} className="p-1 text-[10px] font-medium text-ghost" title={l}>
                {tick(l)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={labels[i]}>
              <td className="pr-2 text-right text-[10px] font-medium text-ghost whitespace-nowrap" title={labels[i]}>
                {tick(labels[i])}
              </td>
              {row.map((v, j) => (
                <td
                  key={j}
                  className="h-8 w-8 rounded text-center text-[10px] tabular-nums text-ink"
                  style={{ background: corrColor(v) }}
                  title={`${labels[i]} · ${labels[j]}: ${v == null ? '—' : v.toFixed(2)}`}
                >
                  {v == null ? '' : v.toFixed(1)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
