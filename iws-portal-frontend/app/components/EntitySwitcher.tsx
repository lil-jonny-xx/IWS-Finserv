'use client';
import { entitiesWithData, useNavCoverage } from '@/app/lib/navCoverage';

// The entity filter pill row, shared by every asset page (each page used to
// carry its own copy — edit HERE only). Pass `section` (the page's nav href)
// or `category` (a Manual Data category, for /assets/<category>) and pills for
// entities with no data in that section are hidden — e.g. ADR never shows on
// Equity while it holds none. A selected entity always keeps its pill so an
// active filter can be cleared.
//
// Two modes:
//   single-select — pass `selectedId` + `onSelect` (legacy; one entity or All).
//   multi-select  — pass `selectedIds` + `onToggle` (empty array = All). Clicking
//                   a pill toggles it; clicking "All" clears the selection. Lets a
//                   user view an arbitrary subset (e.g. ADR + DHR + SDR + IWS).
export default function EntitySwitcher({
  entities, selectedId, onSelect, selectedIds, onToggle, section, category,
}: {
  entities: { id: number; name: string }[];
  selectedId?: number | null;
  onSelect?: (id: number | null) => void;
  selectedIds?: number[];
  onToggle?: (id: number | null) => void;
  section?: string;
  category?: string;
}) {
  const cov = useNavCoverage();
  const ids = category ? cov?.categories[category] : section ? cov?.sections[section] : undefined;
  const shown = entitiesWithData(entities, ids);

  const multi = selectedIds !== undefined && onToggle !== undefined;
  const isActive = (id: number) => multi ? selectedIds!.includes(id) : id === selectedId;
  const allActive = multi ? selectedIds!.length === 0 : selectedId == null;

  // Keep any selected entity's pill visible even if coverage would hide it.
  const selectedSet = multi ? selectedIds! : (selectedId != null ? [selectedId] : []);
  const missing = entities.filter(e => selectedSet.includes(e.id) && !shown.some(s => s.id === e.id));
  const withSelected = missing.length ? [...shown, ...missing] : shown;

  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null as number | null, name: 'All' }, ...withSelected].map(tab => {
        const active = tab.id === null ? allActive : isActive(tab.id);
        return (
          <button key={tab.id ?? 'all'} role="tab" aria-selected={active}
            onClick={() => (multi ? onToggle! : onSelect!)(tab.id)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}
