// Renders a single ChartSpec inside a framed card, switching on chart_type.
import type { ChartSpec } from '../../types';
import DonutChart from './DonutChart';
import BarChart from './BarChart';
import LineChart from './LineChart';
import Heatmap from './Heatmap';

export default function Chart({ spec }: { spec: ChartSpec }) {
  let body: React.ReactNode = null;
  switch (spec.chart_type) {
    case 'donut':
      body = <DonutChart series={spec.series} unit={spec.unit} />;
      break;
    case 'bar':
      body = <BarChart series={spec.series} unit={spec.unit} />;
      break;
    case 'line':
      body = <LineChart points={spec.points} unit={spec.unit} />;
      break;
    case 'heatmap':
      body = <Heatmap labels={spec.labels} matrix={spec.matrix} />;
      break;
    default:
      return null;
  }

  return (
    <figure className="my-3 rounded-lg border border-rule bg-card p-4">
      {spec.title && (
        <figcaption className="mb-3 text-xs font-medium uppercase tracking-wide text-ghost">
          {spec.title}
        </figcaption>
      )}
      {body}
    </figure>
  );
}

export function ChartList({ charts }: { charts?: ChartSpec[] | null }) {
  if (!charts?.length) return null;
  return (
    <div className="max-w-[90%]">
      {charts.map((c, i) => <Chart key={i} spec={c} />)}
    </div>
  );
}
