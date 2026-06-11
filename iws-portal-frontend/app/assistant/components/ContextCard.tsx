// A quiet header card at the top of the thread showing the active portfolio scope.
import type { Entity } from '../types';

export default function ContextCard({
  scopeEntityId,
  entities,
  isAdmin,
}: {
  scopeEntityId: number | null;
  entities: Entity[];
  isAdmin: boolean;
}) {
  const entity = scopeEntityId != null
    ? entities.find(e => e.id === scopeEntityId)
    : null;
  const scopeLabel = scopeEntityId == null
    ? (isAdmin ? 'Whole family' : 'Your portfolio')
    : (entity?.name ?? `Entity #${scopeEntityId}`);

  return (
    <div className="flex items-center gap-3 rounded-lg border border-rule bg-card px-4 py-3">
      <span
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-prime/10 text-prime"
        aria-hidden="true"
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 3v18h18" />
          <path d="m19 9-5 5-4-4-3 3" />
        </svg>
      </span>
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-wide text-ghost">Advising on</p>
        <p className="truncate text-sm font-medium text-ink">{scopeLabel}</p>
      </div>
    </div>
  );
}
