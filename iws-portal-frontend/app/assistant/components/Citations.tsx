// Source chips rendered under an assistant answer (from web_search citations).
import type { Citation } from '../types';

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

export default function Citations({ items }: { items: Citation[] }) {
  if (!items?.length) return null;

  // De-dupe by URL while preserving order.
  const seen = new Set<string>();
  const unique = items.filter(c => {
    if (!c?.url || seen.has(c.url)) return false;
    seen.add(c.url);
    return true;
  });
  if (!unique.length) return null;

  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      {unique.map((c, i) => {
        const host = hostOf(c.url);
        return (
          <a
            key={`${c.url}-${i}`}
            href={c.url}
            target="_blank"
            rel="noopener noreferrer"
            title={c.title || c.url}
            className="group inline-flex items-center gap-1.5 max-w-xs rounded-full border border-rule bg-page/60 px-2.5 py-1 text-xs text-dim transition-colors hover:border-dim hover:text-ink"
          >
            <img
              src={`https://www.google.com/s2/favicons?domain=${host}&sz=32`}
              alt=""
              width={14}
              height={14}
              className="rounded-sm opacity-80 group-hover:opacity-100"
              loading="lazy"
            />
            <span className="truncate">{c.title || host}</span>
          </a>
        );
      })}
    </div>
  );
}
