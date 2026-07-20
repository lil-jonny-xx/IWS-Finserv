// Freshness stamp for hand-entered figures.
//
// Manual entries (bank balances, forex accounts, manually-tracked foreign equity)
// are only as current as the last time somebody typed them in, and the pages that
// show them render no date — so a three-week-old balance looks exactly as current
// as one entered this morning. `updated_at` is the only signal we have: there is
// no separate statement-date field on manual_input (inception_date exists but is
// never populated for cash rows).
//
// Caveat worth remembering: this measures when the row was last *saved*, not when
// the figure last *changed*. A balance re-entered unchanged looks fresh here.

export const STALE_AFTER_DAYS = 7;

export interface AsOf {
  label: string;      // "2 days ago", "17 days ago", "today"
  days: number;
  stale: boolean;     // past STALE_AFTER_DAYS — render in amber
}

export function asOf(iso: string | null | undefined): AsOf | null {
  if (!iso) return null;
  const then = new Date(iso);
  if (isNaN(then.getTime())) return null;

  // Whole calendar days elapsed, so an entry from late yesterday reads
  // "yesterday" rather than "today" on an early-morning refresh.
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.max(0, Math.round((startOfDay(new Date()) - startOfDay(then)) / 86_400_000));

  const label =
    days === 0 ? 'today' :
    days === 1 ? 'yesterday' :
    `${days} days ago`;

  return { label, days, stale: days > STALE_AFTER_DAYS };
}

export function asOfDate(iso: string | null | undefined): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}
