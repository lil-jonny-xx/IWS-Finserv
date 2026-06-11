// Saved-conversations rail: list, new, open, archive.
import type { Conversation } from '../types';

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diff = Date.now() - then;
  const m = Math.floor(diff / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

export default function ConversationSidebar({
  conversations,
  activeId,
  loading,
  onNew,
  onOpen,
  onArchive,
}: {
  conversations: Conversation[];
  activeId: number | null;
  loading: boolean;
  onNew: () => void;
  onOpen: (id: number) => void;
  onArchive: (id: number) => void;
}) {
  return (
    <aside className="flex w-full shrink-0 flex-col border-rule bg-card sm:w-72 sm:border-r">
      <div className="flex items-center justify-between gap-2 border-b border-rule px-4 py-3">
        <h2 className="text-sm font-semibold text-ink">Conversations</h2>
        <button
          type="button"
          onClick={onNew}
          className="inline-flex items-center gap-1 rounded-md bg-prime px-2.5 py-1.5 text-xs font-medium text-prime-fg transition-colors hover:bg-prime-deep"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
          New
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-2">
        {loading && (
          <div className="space-y-2 p-1" aria-hidden="true">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-12 animate-pulse rounded-md bg-rule/60" />
            ))}
          </div>
        )}

        {!loading && conversations.length === 0 && (
          <p className="px-3 py-6 text-center text-xs text-ghost">
            No conversations yet. Start one with “New”.
          </p>
        )}

        {!loading &&
          conversations.map(c => {
            const active = c.id === activeId;
            return (
              <div
                key={c.id}
                className={`group relative mb-1 rounded-md border transition-colors ${
                  active
                    ? 'border-rule bg-page'
                    : 'border-transparent hover:bg-page/70'
                }`}
              >
                <button
                  type="button"
                  onClick={() => onOpen(c.id)}
                  aria-current={active ? 'true' : undefined}
                  className="block w-full px-3 py-2.5 pr-9 text-left"
                >
                  <p className="truncate text-sm font-medium text-ink">
                    {c.title || 'Untitled conversation'}
                  </p>
                  <p className="mt-0.5 text-[11px] text-ghost">{relativeTime(c.updated_at)}</p>
                </button>
                <button
                  type="button"
                  onClick={() => onArchive(c.id)}
                  aria-label="Archive conversation"
                  title="Archive"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1.5 text-ghost opacity-0 transition-opacity hover:text-peril focus:opacity-100 group-hover:opacity-100"
                >
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <rect width="20" height="5" x="2" y="3" rx="1" />
                    <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8M10 12h4" />
                  </svg>
                </button>
              </div>
            );
          })}
      </div>
    </aside>
  );
}
