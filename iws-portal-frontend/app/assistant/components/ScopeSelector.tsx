// Admin-only scope picker shown when starting a new conversation.
// Members are auto-pinned to their own entity server-side, so this never renders for them.
import type { Entity } from '../types';

export default function ScopeSelector({
  entities,
  value,
  onChange,
}: {
  entities: Entity[];
  value: number | null; // null = whole-family
  onChange: (entityId: number | null) => void;
}) {
  const tabs: { id: number | null; name: string }[] = [
    { id: null, name: 'Whole family' },
    ...entities.map(e => ({ id: e.id, name: e.name })),
  ];

  return (
    <div>
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ghost">
        Portfolio scope
      </p>
      <div className="flex flex-wrap gap-1.5" role="radiogroup" aria-label="Portfolio scope">
        {tabs.map(tab => {
          const active = tab.id === value;
          return (
            <button
              key={tab.id ?? 'all'}
              type="button"
              role="radio"
              aria-checked={active}
              onClick={() => onChange(tab.id)}
              className={`rounded px-3 py-1 text-xs font-medium transition-colors ${
                active
                  ? 'bg-prime text-prime-fg'
                  : 'border border-rule bg-card text-dim hover:border-dim hover:text-ink'
              }`}
            >
              {tab.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
