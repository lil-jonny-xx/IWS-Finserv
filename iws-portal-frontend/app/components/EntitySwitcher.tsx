'use client';
import { entitiesWithData, useNavCoverage } from '@/app/lib/navCoverage';

// The entity filter pill row, shared by every asset page (each page used to
// carry its own copy — edit HERE only). Pass `section` (the page's nav href)
// or `category` (a Manual Data category, for /assets/<category>) and pills for
// entities with no data in that section are hidden — e.g. ADR never shows on
// Equity while it holds none. The currently selected entity always keeps its
// pill so an active filter can be cleared.

export default function EntitySwitcher({ entities, selectedId, onSelect, section, category }: {
  entities: { id: number; name: string }[];
  selectedId: number | null;
  onSelect: (id: number | null) => void;
  section?: string;
  category?: string;
}) {
  const cov = useNavCoverage();
  const ids = category ? cov?.categories[category] : section ? cov?.sections[section] : undefined;
  const shown = entitiesWithData(entities, ids);
  const withSelected = selectedId != null && !shown.some(e => e.id === selectedId)
    ? [...shown, ...entities.filter(e => e.id === selectedId)]
    : shown;
  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null as number | null, name: 'All' }, ...withSelected].map(tab => {
        const active = tab.id === selectedId;
        return (
          <button key={tab.id ?? 'all'} role="tab" aria-selected={active}
            onClick={() => onSelect(tab.id)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}
